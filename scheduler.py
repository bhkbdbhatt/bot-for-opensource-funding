"""Rate-limited outreach scheduler.

One *tick* = pick the next batch of uncontacted sponsors, generate a message
for each, dispatch through the matching channel sender, update the tracker.

`run_forever` repeats ticks on a cron-like interval. Every send is persisted
immediately, so a crash mid-tick never re-sends to the same person.
"""

from __future__ import annotations

import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from config_loader import Config
from logging_setup import get_logger
from models import Sponsor
from prompt_engine import generate, plugin_data_from_config, word_count_message, word_limit
from senders import get_sender
from tracker import SponsorTracker

LOG = get_logger("scheduler")


class SchedulerHalted(Exception):
    """Raised after too many consecutive failures - stop and investigate."""


@dataclass
class DeliveryRecord:
    sponsor: str
    channel: str
    ok: bool
    detail: str = ""
    words: int = 0
    dry_run: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sponsor": self.sponsor,
            "channel": self.channel,
            "ok": self.ok,
            "detail": self.detail,
            "words": self.words,
            "dry_run": self.dry_run,
        }


@dataclass
class TickResult:
    attempted: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    deliveries: List[DeliveryRecord] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempted": self.attempted,
            "sent": self.sent,
            "failed": self.failed,
            "skipped": self.skipped,
            "deliveries": [record.to_dict() for record in self.deliveries],
            "notes": self.notes,
        }


class Scheduler:
    """Drives generation + delivery."""

    def __init__(
        self,
        config: Config,
        tracker: SponsorTracker,
        *,
        dry_run: Optional[bool] = None,
        batch_size: Optional[int] = None,
        llm_renderer: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.config = config
        self.tracker = tracker
        self.dry_run = config.bool("scheduler.dry_run", False) if dry_run is None else bool(dry_run)
        self.batch_size = int(
            batch_size if batch_size is not None else config.int("scheduler.batch_size", 5, minimum=1, maximum=100)
        )
        self.interval = config.int("scheduler.interval_seconds", 7200, minimum=1)
        self.llm_renderer = llm_renderer
        self.min_hours_between = config.int("rate_limits.min_hours_between_attempts", 0, maximum=8760)
        self.max_consecutive_failures = config.int("rate_limits.max_consecutive_failures", 5, minimum=1, maximum=100)
        self.consecutive_failures = 0
        self.halted = False
        self._senders: Dict[str, Any] = {}

    # -- helpers ----------------------------------------------------------- #

    def _sender(self, channel: str):
        if channel not in self._senders:
            self._senders[channel] = get_sender(channel, self.config, dry_run=self.dry_run)
        return self._senders[channel]

    def _context(self, sponsor: Sponsor) -> Dict[str, Any]:
        entry = self.tracker.seen_entry(sponsor.github or sponsor.name) or {}
        context: Dict[str, Any] = {
            "github_url": f"https://github.com/{sponsor.github}" if sponsor.github else "",
            "owner_type": sponsor.owner_type,
            "stars": sponsor.stars,
            "top_repo": entry.get("top_repo", ""),
            "genesys_repos_count": entry.get("genesys_repos_count", 0),
            "repos_count": entry.get("repos_count", 0),
            "notes": sponsor.notes,
        }
        return {key: value for key, value in context.items() if value not in ("", None)}

    def build_message(self, sponsor: Sponsor, channel: Optional[str] = None) -> str:
        channel = (channel or sponsor.channel).lower()
        plugin = plugin_data_from_config(self.config)
        return generate(
            plugin,
            channel,
            sponsor.name,
            llm_renderer=self.llm_renderer,
            sponsor_context=self._context(sponsor),
        )

    # -- tick -------------------------------------------------------------- #

    def tick(self) -> TickResult:
        """Process one batch. Never raises for per-sponsor problems."""
        result = TickResult()
        LOG.info(
            "Tick start | batch=%d dry_run=%s usage=%s",
            self.batch_size,
            self.dry_run,
            self.tracker.daily_usage(),
        )

        candidates = self.tracker.next_batch(self.batch_size)
        if not candidates:
            result.notes.append("no sponsors in 'new' status")
            LOG.info("Nothing to send: no sponsors in 'new' status")
            return result

        return self._deliver(candidates, result)

    def deliver(self, sponsors: List[Sponsor]) -> TickResult:
        """Deliver to an explicit list, honouring the same gates as `tick()`.

        Used by the web UI to send exactly the sponsors a human approved,
        rather than the rank-ordered `next_batch`.
        """
        result = TickResult()
        return self._deliver(list(sponsors), result)

    def _deliver(self, candidates: List[Sponsor], result: TickResult) -> TickResult:
        for sponsor in candidates:
            if self.halted:
                result.notes.append("scheduler halted after repeated failures")
                break

            channel = sponsor.channel
            if not self.config.channel_enabled(channel):
                result.skipped += 1
                result.notes.append(f"{channel} channel disabled in config")
                LOG.warning("Skipping %s: %s channel disabled", sponsor.name, channel)
                continue

            allowed, reason = self.tracker.can_send(
                sponsor, min_hours_between_attempts=self.min_hours_between
            )
            if not allowed:
                result.skipped += 1
                result.notes.append(f"{sponsor.name}: {reason}")
                LOG.info("Skipping %s: %s", sponsor.name, reason)
                continue

            sender = self._sender(channel)
            deliverable, deliver_reason = sender.can_send(sponsor)
            if not deliverable:
                result.skipped += 1
                result.notes.append(f"{sponsor.name}: {deliver_reason}")
                LOG.warning("Not deliverable to %s: %s", sponsor.name, deliver_reason)
                continue

            try:
                message = self.build_message(sponsor)
            except Exception as exc:  # noqa: BLE001 - generation must not kill the tick
                result.failed += 1
                self.consecutive_failures += 1
                record = DeliveryRecord(sponsor.name, channel, False, f"generation failed: {exc}")
                result.deliveries.append(record)
                LOG.exception("Message generation failed for %s", sponsor.name)
                self._persist_failure(sponsor, channel, record.detail)
                continue

            words = word_count_message(channel, message)
            limit = word_limit(channel)
            if words > limit:
                LOG.warning("Generated message for %s is %d words (limit %d)", sponsor.name, words, limit)

            result.attempted += 1
            try:
                ok = bool(sender.send(sponsor, message))
                detail = "ok" if ok else "sender reported failure"
            except Exception as exc:  # noqa: BLE001 - sender must not kill the tick
                ok, detail = False, f"{type(exc).__name__}: {exc}"
                LOG.exception("Sender raised for %s", sponsor.name)

            record = DeliveryRecord(
                sponsor=sponsor.name,
                channel=channel,
                ok=ok,
                detail=detail,
                words=words,
                dry_run=self.dry_run,
            )
            result.deliveries.append(record)

            try:
                self.tracker.record_send(sponsor.name, channel, ok=ok, error="" if ok else detail)
                self.tracker.save()
            except Exception as exc:  # noqa: BLE001 - state must not be lost silently
                LOG.exception("Could not record delivery for %s: %s", sponsor.name, exc)

            if ok:
                result.sent += 1
                self.consecutive_failures = 0
            else:
                result.failed += 1
                self.consecutive_failures += 1
                LOG.error("Delivery failed for %s: %s", sponsor.name, detail)

            if self.consecutive_failures >= self.max_consecutive_failures:
                self.halted = True
                result.notes.append(
                    f"{self.consecutive_failures} consecutive failures - halting the scheduler"
                )
                LOG.error(
                    "Halting: %d consecutive delivery failures (rate_limits.max_consecutive_failures)",
                    self.consecutive_failures,
                )
                break

        LOG.info(
            "Tick end | attempted=%d sent=%d failed=%d skipped=%d usage=%s",
            result.attempted,
            result.sent,
            result.failed,
            result.skipped,
            self.tracker.daily_usage(),
        )
        return result

    def _persist_failure(self, sponsor: Sponsor, channel: str, error: str) -> None:
        try:
            self.tracker.record_send(sponsor.name, channel, ok=False, error=error)
            self.tracker.save()
        except Exception:  # noqa: BLE001
            LOG.exception("Could not record generation failure for %s", sponsor.name)

    # -- loop -------------------------------------------------------------- #

    def run_forever(
        self,
        *,
        interval: Optional[int] = None,
        max_ticks: Optional[int] = None,
        stop_event: Optional[threading.Event] = None,
        run_immediately: bool = True,
    ) -> List[TickResult]:
        """Cron-like loop. Ctrl-C (SIGINT/SIGTERM) stops it cleanly."""
        wait = max(int(interval if interval is not None else self.interval), 1)
        stop = stop_event or threading.Event()
        self._install_signal_handlers(stop)

        results: List[TickResult] = []
        tick_no = 0
        LOG.info(
            "Scheduler online | interval=%ds batch=%d dry_run=%s max_ticks=%s",
            wait,
            self.batch_size,
            self.dry_run,
            max_ticks if max_ticks is not None else "unlimited",
        )

        try:
            while not stop.is_set():
                if run_immediately or tick_no > 0:
                    tick_no += 1
                    results.append(self.tick())
                    if self.halted:
                        LOG.error("Scheduler halted. Fix the delivery errors, then restart.")
                        break
                if max_ticks is not None and tick_no >= max_ticks:
                    LOG.info("Reached max_ticks=%d, stopping", max_ticks)
                    break
                LOG.info("Sleeping %ds until the next tick", wait)
                if self._sleep(wait, stop):
                    break
        except KeyboardInterrupt:
            LOG.info("Interrupted by user; stopping after %d tick(s)", tick_no)
        finally:
            self._restore_signal_handlers()
            LOG.info("Scheduler stopped")
        return results

    def _sleep(self, seconds: int, stop: threading.Event) -> bool:
        """Interruptible sleep. Returns True if a stop was requested."""
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
                LOG.debug("Could not install handler for %s", signame)

    def _restore_signal_handlers(self) -> None:
        for signum, handler in getattr(self, "_previous_handlers", {}).items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError, TypeError):
                pass
        self._previous_handlers = {}


def bootstrap(config: Config, *, dry_run: Optional[bool] = None, batch_size: Optional[int] = None) -> Scheduler:
    """Build a Scheduler with a tracker seeded from config."""
    tracker = SponsorTracker(
        config.path("paths.tracker_file", "sponsors.json"),
        seed_sponsors=config.records("sponsors"),
        rate_limits=config.rate_limits,
    )
    return Scheduler(config, tracker, dry_run=dry_run, batch_size=batch_size)