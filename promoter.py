"""Content syndication scheduler.

The counterpart to `scheduler.py`, and deliberately the same shape so the two
pipelines are operationally interchangeable: one *tick* picks a batch of
(article, platform) pairs, checks the circuit breaker, the platform's readiness,
the store's daily caps and cooldown, and the content gate, then shapes, sends and
**persists after every single publish**. `run_forever` is the same cron-like loop
with the same interruptible sleep and signal handling.

What differs, and why:

* **A batch is pairs, not sponsors.** One article fanned out to six platforms is
  six separately accounted operations, each with its own URL, its own retry
  history and its own quota. A failure on CoderLegion must not roll back a
  successful WordPress publish.
* **The gate is a hard precondition.** `Scheduler` warns about a word budget;
  here, publishing an article that violates `content.min_words`, omits the
  project link or has no author disclosure is *refused*, because these
  communities remove content that breaks their rules and that removal is visible.
  `publishing.enforce_quality_gate: false` downgrades this to a warning for
  operators who accept the risk.
* **Manual targets are a first-class outcome, not a fallback.** `queued` is a
  successful tick, not a failure.
* **Dry runs never touch the quota.** A rehearsal must be repeatable.
"""

from __future__ import annotations

import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from config_loader import Config
from content_engine import content_data_from_config, generate, prepare_for_platform
from content_store import ContentStore, PublishTarget
from logging_setup import get_logger
from models import (
    CONTENT_APPROVED,
    CONTENT_DRAFT,
    CONTENT_QUEUED,
    CONTENT_STATUSES,
    MODE_DRY_RUN,
    MODE_MANUAL,
    PLATFORM_IDS,
    ContentItem,
)
from publishers import get_publisher

LOG = get_logger("promoter")

DEFAULT_BATCH_SIZE = 3
DEFAULT_INTERVAL = 10800          # 3 hours - slower than outreach on purpose
DEFAULT_COOLDOWN_HOURS = 24
DEFAULT_DELAY_SECONDS = 2.0
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class PublishRecord:
    """One (article, platform) outcome, as reported by a tick."""

    item_id: str
    title: str
    platform: str
    ok: bool
    mode: str = ""
    live: bool = False
    url: str = ""
    detail: str = ""
    words: int = 0
    dry_run: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "platform": self.platform,
            "ok": self.ok,
            "mode": self.mode,
            "live": self.live,
            "url": self.url,
            "detail": self.detail,
            "words": self.words,
            "dry_run": self.dry_run,
        }


@dataclass
class TickResult:
    attempted: int = 0
    published: int = 0
    drafted: int = 0
    queued: int = 0
    failed: int = 0
    skipped: int = 0
    records: List[PublishRecord] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempted": self.attempted,
            "published": self.published,
            "drafted": self.drafted,
            "queued": self.queued,
            "failed": self.failed,
            "skipped": self.skipped,
            "records": [record.to_dict() for record in self.records],
            "notes": self.notes,
        }


class Promoter:
    """Drives content rendering + syndication."""

    def __init__(
        self,
        config: Config,
        store: ContentStore,
        *,
        dry_run: Optional[bool] = None,
        batch_size: Optional[int] = None,
        llm_renderer: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.config = config
        self.store = store
        self.dry_run = (
            config.bool("publishing.dry_run", False) if dry_run is None else bool(dry_run)
        )
        self.batch_size = int(
            batch_size
            if batch_size is not None
            else config.int("publishing.batch_size", DEFAULT_BATCH_SIZE, minimum=1, maximum=50)
        )
        self.interval = config.int(
            "publishing.interval_seconds", DEFAULT_INTERVAL, minimum=60, maximum=604800
        )
        self.platforms = self._enabled_platforms()
        self.llm_renderer = llm_renderer
        self.min_hours_between = config.int(
            "rate_limits.min_hours_between_platform_posts",
            DEFAULT_COOLDOWN_HOURS,
            maximum=8760,
        )
        self.max_consecutive_failures = config.int(
            "rate_limits.max_content_failures", MAX_CONSECUTIVE_FAILURES, minimum=1, maximum=100
        )
        self.enforce_gate = config.bool("publishing.enforce_quality_gate", True)
        self.require_approval = config.bool("publishing.require_approval", True)
        self.publishable_statuses = (
            (CONTENT_APPROVED, CONTENT_QUEUED)
            if self.require_approval
            else (CONTENT_DRAFT, CONTENT_APPROVED, CONTENT_QUEUED)
        )
        self.request_delay = config.float(
            "publishing.request_delay_seconds", DEFAULT_DELAY_SECONDS
        )
        self.consecutive_failures = 0
        self.halted = False
        self._publishers: Dict[str, Any] = {}
        self._last_sent = 0.0

    # -- helpers ----------------------------------------------------------- #

    def _enabled_platforms(self) -> List[str]:
        """Platforms this campaign actually wants to publish to.

        `platforms.<id>.enabled` is required to be explicitly true. A campaign
        that omits the section entirely targets nothing, which is the safe
        default: publishing is a public act and must be opted into.
        """
        return [
            platform
            for platform in PLATFORM_IDS
            if self.config.bool(f"platforms.{platform}.enabled", False)
        ]

    def publisher(self, platform: str):
        if platform not in self._publishers:
            self._publishers[platform] = get_publisher(
                platform, self.config, dry_run=self.dry_run
            )
        return self._publishers[platform]

    def content_data(self) -> Dict[str, Any]:
        return content_data_from_config(self.config)

    def render(self, item: ContentItem, platform: str) -> Dict[str, Any]:
        """Shape one article for one platform, running the quality gate."""
        return prepare_for_platform(
            self.content_data(),
            title=item.title,
            body=item.body_markdown,
            summary=item.summary,
            tags=item.tags,
            platform=platform,
        )

    def compose_item(
        self, *, title: str = "", topic: str = "", platforms: Optional[List[str]] = None
    ) -> ContentItem:
        """Build a new article from config and return it unsaved."""
        data = self.content_data()
        draft = generate(
            data,
            title=title,
            topic=topic,
            platform=(platforms or [""])[0],
            llm_renderer=self.llm_renderer,
        )
        targets = [pid for pid in (platforms or []) if pid in PLATFORM_IDS]
        item = ContentItem(
            id=ContentStore.make_id(draft["title"], topic),
            title=draft["title"],
            body_markdown=draft["body"],
            summary=draft["summary"],
            topic=topic,
            platforms=targets,
            tags=[str(tag) for tag in (data.get("tags") or [])],
            canonical_url=str(data.get("canonical_base_url") or ""),
            status=CONTENT_DRAFT,
            source="composed",
            words=int(draft.get("words") or 0),
        )
        item.fingerprint = item.compute_fingerprint()
        return item

    # -- tick -------------------------------------------------------------- #

    def tick(
        self,
        *,
        platforms: Optional[List[str]] = None,
        item_ids: Optional[List[str]] = None,
    ) -> TickResult:
        """Process one batch. Never raises for per-item problems."""
        result = TickResult()
        wanted = [pid for pid in (platforms or self.platforms) if pid in PLATFORM_IDS]
        if not wanted:
            result.notes.append(
                "no platforms enabled - set platforms.<id>.enabled: true to publish"
            )
            LOG.info("Nothing to publish: no platforms are enabled")
            return result

        targets = self.store.next_targets(
            self.batch_size,
            platforms=wanted,
            item_ids=item_ids,
            statuses=self.publishable_statuses,
        )
        if not targets:
            result.notes.append(
                "no approved articles waiting - run 'content draft' then 'content approve'"
            )
            LOG.info("Nothing to publish: no approved articles with pending targets")
            return result

        LOG.info(
            "Publish tick start | batch=%d dry_run=%s usage=%s",
            self.batch_size,
            self.dry_run,
            self.store.daily_usage(),
        )
        return self._publish_all(targets, result)

    def publish_targets(self, targets: List[PublishTarget]) -> TickResult:
        """Publish an explicit list of pairs (used by the web UI)."""
        return self._publish_all(list(targets), TickResult())

    def _publish_all(self, targets: List[PublishTarget], result: TickResult) -> TickResult:
        for target in targets:
            if self.halted:
                result.notes.append("promoter halted after repeated failures")
                break

            item = target.item
            platform = target.platform
            label = f"{item.id} -> {platform}"

            if platform not in self.platforms:
                result.skipped += 1
                result.notes.append(f"{label}: platforms.{platform}.enabled is false")
                continue

            allowed, reason = self.store.can_publish(
                target, min_hours_between=self.min_hours_between
            )
            if not allowed:
                result.skipped += 1
                result.notes.append(f"{label}: {reason}")
                LOG.info("Skipping %s: %s", label, reason)
                continue

            try:
                publisher = self.publisher(platform)
            except Exception as exc:  # noqa: BLE001 - a bad target must not kill the tick
                result.failed += 1
                self.consecutive_failures += 1
                result.records.append(
                    PublishRecord(item.id, item.title, platform, False, detail=f"{type(exc).__name__}: {exc}")
                )
                self._persist(target, ok=False, mode="", detail=f"publisher unavailable: {exc}")
                continue

            deliverable, deliver_reason = publisher.can_publish(item)
            if not deliverable:
                result.skipped += 1
                result.notes.append(f"{label}: {deliver_reason}")
                LOG.warning("Not publishable to %s: %s", platform, deliver_reason)
                continue

            try:
                payload = self.render(item, platform)
            except Exception as exc:  # noqa: BLE001
                result.failed += 1
                self.consecutive_failures += 1
                detail = f"rendering failed: {exc}"
                result.records.append(
                    PublishRecord(item.id, item.title, platform, False, detail=detail)
                )
                self._persist(target, ok=False, mode="", detail=detail)
                continue

            problems = self._gate_problems(target, payload)
            if problems:
                message = "; ".join(problems)
                result.skipped += 1
                result.notes.append(f"{label}: quality gate - {message}")
                LOG.warning("Quality gate blocked %s: %s", label, message)
                self._block(target, message)
                continue

            self._pace()

            result.attempted += 1
            try:
                outcome = publisher.publish(item, payload)
            except Exception as exc:  # noqa: BLE001 - publisher must not kill the tick
                LOG.exception("Publisher raised for %s", label)
                outcome = _failed_outcome(platform, f"{type(exc).__name__}: {exc}", publisher.mode)

            record = PublishRecord(
                item_id=item.id,
                title=item.title,
                platform=platform,
                ok=outcome.ok,
                mode=outcome.mode,
                live=outcome.live,
                url=outcome.url,
                detail=outcome.detail,
                words=outcome.words,
                dry_run=outcome.dry_run,
            )
            result.records.append(record)

            self._persist(
                target,
                ok=outcome.ok,
                mode=outcome.mode,
                live=outcome.live,
                url=outcome.url,
                external_id=outcome.external_id,
                detail=outcome.detail,
                count_against_quota=outcome.mode != MODE_DRY_RUN,
            )

            if outcome.ok:
                self.consecutive_failures = 0
                self._last_sent = time.monotonic()
                if outcome.mode == MODE_DRY_RUN:
                    result.notes.append(f"{label}: dry run only")
                elif outcome.mode == MODE_MANUAL:
                    result.queued += 1
                elif outcome.live:
                    result.published += 1
                else:
                    result.drafted += 1
            else:
                result.failed += 1
                self.consecutive_failures += 1
                LOG.error("Publish failed for %s: %s", label, outcome.detail)

            if self.consecutive_failures >= self.max_consecutive_failures:
                self.halted = True
                result.notes.append(
                    f"{self.consecutive_failures} consecutive failures - halting the promoter"
                )
                LOG.error(
                    "Halting: %d consecutive publish failures "
                    "(rate_limits.max_content_failures)",
                    self.consecutive_failures,
                )
                break

        LOG.info(
            "Publish tick end | attempted=%d published=%d drafted=%d queued=%d failed=%d skipped=%d",
            result.attempted,
            result.published,
            result.drafted,
            result.queued,
            result.failed,
            result.skipped,
        )
        return result

    # -- gates ------------------------------------------------------------- #

    def _gate_problems(self, target: PublishTarget, payload: Dict[str, Any]) -> List[str]:
        """The quality gate, unless the operator has explicitly downgraded it."""
        problems = list(payload.get("problems") or [])
        if not problems:
            return []
        if not self.enforce_gate:
            LOG.warning(
                "Quality gate advisory only for %s: %s", target.platform, "; ".join(problems)
            )
            return []
        return problems

    def _pace(self) -> None:
        """Space outbound requests so a burst never looks like a scraper."""
        wait = float(self.request_delay or 0)
        if wait <= 0:
            return
        elapsed = time.monotonic() - self._last_sent
        if self._last_sent and elapsed < wait:
            time.sleep(wait - elapsed)

    def _block(self, target: PublishTarget, reason: str) -> None:
        """Record a gate refusal without counting it as a publish attempt."""
        try:
            self.store.record_blocked(target, reason)
            self.store.save()
        except Exception:  # noqa: BLE001
            LOG.exception("Could not record the blocked publication for %s", target.item_id)

    def _persist(
        self,
        target: PublishTarget,
        *,
        ok: bool,
        mode: str = "",
        live: bool = True,
        url: str = "",
        external_id: str = "",
        detail: str = "",
        count_against_quota: bool = True,
    ) -> None:
        """Record and immediately save. A crash here must not re-publish.

        A **dry run writes nothing at all**. Rehearsing must be repeatable: if
        it recorded an attempt it would consume quota, arm the platform cooldown
        and stamp `last_attempt_at`, so the first *real* run after a rehearsal
        would be skipped. (The outreach scheduler does consume quota on a dry
        run; that is a wart of the older pipeline, not something to copy.)
        """
        if mode == MODE_DRY_RUN:
            LOG.info(
                "[DRY RUN] %s -> %s (%s words) - ledger untouched",
                target.item_id,
                target.platform,
                detail or "payload shaped",
            )
            return
        try:
            self.store.record_publish(
                target,
                ok=ok,
                mode=mode,
                live=live,
                url=url,
                external_id=external_id,
                detail=detail,
                count_against_quota=count_against_quota,
            )
            self.store.save()
        except Exception:  # noqa: BLE001 - state loss must be loud but not fatal
            LOG.exception("Could not record publication for %s/%s", target.item_id, target.platform)

    # -- loop -------------------------------------------------------------- #

    def run_forever(
        self,
        *,
        interval: Optional[int] = None,
        max_ticks: Optional[int] = None,
        stop_event: Optional[threading.Event] = None,
        platforms: Optional[List[str]] = None,
        run_immediately: bool = True,
    ) -> List[TickResult]:
        """Cron-like loop. Ctrl-C (SIGINT/SIGTERM) stops it cleanly."""
        wait = max(int(interval if interval is not None else self.interval), 60)
        stop = stop_event or threading.Event()
        self._install_signal_handlers(stop)

        results: List[TickResult] = []
        tick_no = 0
        LOG.info(
            "Promoter online | interval=%ds batch=%d dry_run=%s platforms=%s",
            wait,
            self.batch_size,
            self.dry_run,
            ", ".join(platforms or self.platforms) or "none",
        )
        try:
            while not stop.is_set():
                if run_immediately or tick_no > 0:
                    tick_no += 1
                    results.append(self.tick(platforms=platforms))
                    if self.halted:
                        LOG.error("Promoter halted. Fix the errors, then restart.")
                        break
                if max_ticks is not None and tick_no >= max_ticks:
                    LOG.info("Reached max_ticks=%d, stopping", max_ticks)
                    break
                LOG.info("Sleeping %ds until the next publish tick", wait)
                if self._sleep(wait, stop):
                    break
        except KeyboardInterrupt:
            LOG.info("Interrupted by user; stopping after %d tick(s)", tick_no)
        finally:
            self._restore_signal_handlers()
            LOG.info("Promoter stopped")
        return results

    def _sleep(self, seconds: int, stop: threading.Event) -> bool:
        deadline = time.monotonic() + seconds
        while not stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if stop.wait(timeout=min(remaining, 1.0)):
                return True
        return True

    # -- signals ----------------------------------------------------------- #

    def _install_signal_handlers(self, stop: threading.Event) -> None:
        self._previous_handlers: Dict[int, Any] = {}

        def _handler(signum, _frame):  # noqa: ANN001
            LOG.info("Received signal %s - shutting down after the current tick", signum)
            stop.set()

        for signame in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, signame, None)
            if sig is None:
                continue
            try:
                self._previous_handlers[int(sig)] = signal.signal(sig, _handler)
            except (ValueError, OSError):  # not on the main thread
                LOG.debug("Could not install handler for %s", sig)

    def _restore_signal_handlers(self) -> None:
        for signum, handler in getattr(self, "_previous_handlers", {}).items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError, TypeError):
                pass
        self._previous_handlers = {}


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #


def _failed_outcome(platform: str, detail: str, mode: str = ""):
    from publishers.base import PublishOutcome

    return PublishOutcome.failure(platform, detail, mode=mode)


def build_store(config: Config) -> ContentStore:
    """ContentStore with the per-platform and global caps resolved."""
    from content_store import GLOBAL_KEY
    from platforms import PLATFORMS

    limits: Dict[str, int] = {}
    explicit = config.section("rate_limits.platform_daily_limits")
    for platform in PLATFORM_IDS:
        configured = None
        if isinstance(explicit, dict):
            configured = explicit.get(platform)
        if configured is None:
            limits[platform] = PLATFORMS[platform].default_daily_limit
        else:
            try:
                limits[platform] = max(int(configured), 0)
            except (TypeError, ValueError):
                limits[platform] = PLATFORMS[platform].default_daily_limit
    limits[GLOBAL_KEY] = config.int(
        "rate_limits.max_publishes_per_day", 0, minimum=0, maximum=1000
    )
    return ContentStore(
        config.path("paths.content_file", "content.json"),
        rate_limits=limits,
    )


def bootstrap(
    config: Config, *, dry_run: Optional[bool] = None, batch_size: Optional[int] = None
) -> Promoter:
    """Build a Promoter wired to a fresh ContentStore."""
    return Promoter(config, build_store(config), dry_run=dry_run, batch_size=batch_size)


def describe_status(statuses: Optional[List[str]] = None) -> Dict[str, str]:
    """Status -> meaning, for `content list` and the UI."""
    from content_engine import status_help

    help_text = status_help()
    if not statuses:
        return help_text
    return {status: help_text.get(status, "") for status in statuses if status in CONTENT_STATUSES}
