"""JSON-backed content syndication ledger.

The counterpart to `tracker.py`, for the second pipeline: articles instead of
sponsors, platforms instead of channels. It shares `state.py` for the two
properties that must never regress - atomic writes and self-healing reads - and
adds the three things syndication needs and outreach does not:

* **A fan-out ledger.** `ContentItem.publications` records one outcome per
  (article, platform) pair, so "published" means every target is live and the
  article's canonical URLs are on record.
* **A status machine with an explicit human step.** Nothing reaches `approved`
  without a person, and a platform with no API lands in `queued` - *not*
  `published` - until someone confirms the live URL. This is deliberately
  stricter than `ForumSender`'s manual mode, which marks a sponsor `contacted`
  on the strength of a prepared post; here the ledger must not claim a URL that
  does not exist yet.
* **Two rate-limit dimensions.** A per-platform daily cap (each community has
  its own tolerance for volume) plus a global `all` cap, so a campaign cannot
  spray one article across six platforms in a single minute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from logging_setup import get_logger
from models import (
    CONTENT_APPROVED,
    CONTENT_DRAFT,
    CONTENT_FAILED,
    CONTENT_PIPELINE_STATUSES,
    CONTENT_PUBLISHED,
    CONTENT_QUEUED,
    CONTENT_STATUSES,
    MODE_MANUAL,
    PLATFORM_IDS,
    ContentItem,
    fingerprint,
    normalize_name,
    now_iso,
    slugify,
    today_str,
)
from state import (
    AtomicJsonStore,
    BoundedLog,
    DailyCounters,
    StateError,
    cooldown_reason,
)

LOG = get_logger("content-store")

SCHEMA_VERSION = 1
MAX_HISTORY = 500
MAX_ID_LEN = 60

#: Key under which the cross-platform daily cap is tracked.
GLOBAL_KEY = "all"


class ContentStoreError(Exception):
    """Raised for unusable content state."""


class ContentNotFound(ContentStoreError):
    def __init__(self, item_id: str) -> None:
        super().__init__(f"content item not found: {item_id!r}")
        self.item_id = item_id


# --------------------------------------------------------------------------- #
# Publish target
# --------------------------------------------------------------------------- #


@dataclass
class PublishTarget:
    """One (article, platform) pair - the atomic unit of syndication.

    A single tick processes a batch of these, so authoring one article and
    fanning it out to six platforms is six independent, separately accounted,
    separately recoverable operations.
    """

    item: ContentItem
    platform: str
    mode: str = ""
    publishable: bool = True
    problems: List[str] = field(default_factory=list)

    @property
    def item_id(self) -> str:
        return self.item.id

    @property
    def title(self) -> str:
        return self.item.title

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "platform": self.platform,
            "mode": self.mode,
            "publishable": self.publishable,
            "problems": list(self.problems),
        }


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class ContentStore(AtomicJsonStore):
    """In-memory view over the content ledger file."""

    schema_version = SCHEMA_VERSION

    def __init__(
        self,
        path: str | Path,
        *,
        rate_limits: Optional[Dict[str, int]] = None,
    ) -> None:
        super().__init__(path)
        self.rate_limits: Dict[str, int] = dict(rate_limits or {})
        for platform in PLATFORM_IDS:
            self.rate_limits.setdefault(platform, 0)
        self.rate_limits.setdefault(GLOBAL_KEY, 0)

        self._items: Dict[str, ContentItem] = {}
        self._history = BoundedLog([], limit=MAX_HISTORY)
        self._counters = DailyCounters(keys=list(self.rate_limits))
        self._load()
        if not self.path.exists():
            self.save()

    # -- persistence ------------------------------------------------------- #

    def _document(self) -> Dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "items": [self._items[key].to_dict() for key in sorted(self._items)],
            "rate_counters": self._counters.to_dict(),
            "history": self._history.to_list(),
        }

    def _load(self) -> None:
        state = self.load()
        if not state:
            return

        for raw in state.get("items") or []:
            try:
                item = ContentItem.from_dict(raw)
            except (ValueError, TypeError) as exc:
                LOG.error("Skipping malformed content entry: %s", exc)
                continue
            if not item.id or not item.title:
                continue
            # An entry can never have been published if no platform ever
            # recorded an attempt - clear the stamp so the cooldown cannot arm
            # itself against a never-published article.
            if not any(entry.attempts for entry in item.publications.values()):
                item.last_published_at = ""
            self._items[item.key] = item

        counters = state.get("rate_counters")
        if isinstance(counters, dict):
            self._counters = DailyCounters(counters, keys=list(self.rate_limits))
        history = state.get("history")
        if isinstance(history, list):
            self._history = BoundedLog(history, limit=MAX_HISTORY)
        self._counters.roll()

    def save(self) -> None:
        """Atomically persist, translating the low-level failure into ours."""
        try:
            super().save()
        except StateError as exc:
            raise ContentStoreError(str(exc)) from exc

    # -- helpers ----------------------------------------------------------- #

    def _record(self, event: str, **fields: Any) -> None:
        self._history.append(event, **fields)

    @staticmethod
    def make_id(title: str, topic: str = "") -> str:
        """Deterministic item id, so re-running `draft` finds the same article.

        Combines topic and title without repeating an identical slug (which
        happens constantly, because the composer falls back to the topic when no
        title is given), then trims to :data:`MAX_ID_LEN` on a hyphen boundary
        with a short digest appended so two long titles cannot collide.
        """
        parts: List[str] = []
        for candidate in (slugify(topic, fallback="") if topic else "", slugify(title, fallback="")):
            if candidate and candidate not in parts:
                parts.append(candidate)
        base = "-".join(parts) or f"post-{today_str()}"
        if len(base) <= MAX_ID_LEN:
            return base
        digest = fingerprint(title, topic)[:6]
        head = base[: MAX_ID_LEN - len(digest) - 1]
        head = head.rsplit("-", 1)[0] if "-" in head else head
        return f"{head}-{digest}"

    # -- CRUD -------------------------------------------------------------- #

    def add(self, item: ContentItem, *, replace_body: bool = True) -> Tuple[ContentItem, bool]:
        """Insert or refresh an article. Returns ``(item, created)``.

        Refresh is *not* destructive: a re-drafted body overwrites the old one
        (that is the point of re-running `draft`), but publication history,
        approval state and error bookkeeping are preserved so a tweak never
        loses the record of what already went live where.
        """
        if not item.id:
            item.id = self.make_id(item.title)
        errors = item.validate()
        if errors:
            raise ContentStoreError("; ".join(errors))

        existing = self._items.get(item.key)
        if existing is None:
            self._items[item.key] = item
            self._record("added", item=item.id, title=item.title)
            LOG.info("Added content item %s (%s)", item.id, item.status)
            return item, True

        if replace_body and item.body_markdown.strip():
            existing.body_markdown = item.body_markdown
            existing.summary = item.summary or existing.summary
            existing.tags = item.tags or existing.tags
            existing.fingerprint = item.fingerprint or existing.fingerprint
            existing.words = item.words or existing.words
        if item.platforms:
            merged = list(existing.platforms)
            for platform in item.platforms:
                if platform not in merged:
                    merged.append(platform)
            existing.platforms = merged
        if item.canonical_url:
            existing.canonical_url = item.canonical_url
        if item.notes:
            existing.notes = item.notes
        if item.topic:
            existing.topic = item.topic
        existing.updated_at = now_iso()
        existing.sync_status()
        self._record("updated", item=existing.id)
        LOG.info("Refreshed content item %s", existing.id)
        return existing, False

    def get(self, item_id: str) -> Optional[ContentItem]:
        if not item_id:
            return None
        return self._items.get(normalize_name(item_id))

    def require(self, item_id: str) -> ContentItem:
        item = self.get(item_id)
        if item is None:
            raise ContentNotFound(item_id)
        return item

    def remove(self, item_id: str) -> ContentItem:
        item = self.require(item_id)
        del self._items[item.key]
        self._record("removed", item=item.id)
        LOG.info("Removed content item %s", item.id)
        return item

    def all(
        self,
        *,
        statuses: Optional[Iterable[str]] = None,
        platform: Optional[str] = None,
    ) -> List[ContentItem]:
        wanted = {status.lower() for status in statuses} if statuses else None
        items = list(self._items.values())
        if wanted:
            items = [item for item in items if item.status in wanted]
        if platform:
            target = platform.strip().lower()
            items = [item for item in items if target in item.platforms]
        return sorted(items, key=lambda item: (item.created_at, item.id))

    def find_by_fingerprint(self, digest: str) -> Optional[ContentItem]:
        if not digest:
            return None
        for item in self._items.values():
            if item.fingerprint == digest:
                return item
        return None

    # -- pipeline ---------------------------------------------------------- #

    def update_status(self, item_id: str, status: str, *, note: str = "") -> ContentItem:
        status = (status or "").strip().lower()
        if status not in CONTENT_STATUSES:
            raise ContentStoreError(
                f"invalid status {status!r}; expected one of {', '.join(CONTENT_STATUSES)}"
            )
        item = self.require(item_id)
        previous = item.status
        item.status = status
        item.updated_at = now_iso()
        if status == CONTENT_APPROVED and not item.approved_at:
            item.approved_at = now_iso()
        if status != CONTENT_FAILED:
            item.last_error = ""
        # Reverting to draft/approved clears stale hand-off state so the item can
        # legitimately be published again.
        if status in {CONTENT_DRAFT, CONTENT_APPROVED}:
            for entry in item.publications.values():
                if entry.status in {"manual", "failed"}:
                    entry.status = "pending"
                    entry.detail = ""
                    entry.last_error = ""
                    entry.updated_at = now_iso()
        self._record("status", item=item.id, **{"from": previous, "to": status})
        LOG.info(
            "Content status %s: %s -> %s%s", item.id, previous, status, f" ({note})" if note else ""
        )
        return item

    def approve(self, item_id: str) -> ContentItem:
        return self.update_status(item_id, CONTENT_APPROVED, note="approved by operator")

    def next_targets(
        self,
        n: int = 3,
        *,
        platforms: Optional[Iterable[str]] = None,
        item_ids: Optional[Iterable[str]] = None,
        statuses: Optional[Iterable[str]] = None,
    ) -> List[PublishTarget]:
        """Up to ``n`` (article, platform) pairs ready to be sent.

        Ready means: the item is in ``statuses`` (default ``approved`` and
        ``queued``), the platform is still pending for that item, and the
        platform is in the allowed set. Ordering is oldest item first so a
        content calendar drains in the order it was planned.
        """
        allowed = (
            {platform.strip().lower() for platform in platforms}
            if platforms
            else set(PLATFORM_IDS)
        )
        wanted_items = (
            {normalize_name(item_id) for item_id in item_ids} if item_ids else None
        )
        ready = tuple(
            str(status).lower()
            for status in (statuses or (CONTENT_APPROVED, CONTENT_QUEUED))
        )
        targets: List[PublishTarget] = []
        for item in self.all(statuses=ready):
            if wanted_items is not None and item.key not in wanted_items:
                continue
            for platform in item.platforms:
                if platform not in allowed:
                    continue
                publication = item.publication(platform)
                if publication.is_live:
                    continue
                targets.append(PublishTarget(item=item, platform=platform))
                if len(targets) >= max(int(n), 0):
                    return targets
        return targets

    def pending_count(
        self,
        *,
        platforms: Optional[Iterable[str]] = None,
        statuses: Optional[Iterable[str]] = None,
    ) -> int:
        ready = tuple(
            str(status).lower()
            for status in (statuses or (CONTENT_APPROVED, CONTENT_QUEUED))
        )
        return len(self.next_targets(1_000_000, platforms=platforms, statuses=ready))

    # -- rate limiting ----------------------------------------------------- #

    def can_publish(
        self, target: PublishTarget, *, min_hours_between: int = 0
    ) -> Tuple[bool, str]:
        """Rate-limit gate for one (article, platform) pair.

        Order matters: an unconfigured limit is reported as "no capacity"
        rather than being silently treated as unlimited, so a missing
        `rate_limits.platform_daily_limits` entry cannot become an unbounded
        firehose.
        """
        platform = target.platform
        item = target.item

        limit = int(self.rate_limits.get(platform, 0) or 0)
        if limit <= 0:
            return False, f"no daily publish limit configured for {platform}"
        used = self._counters.used(platform)
        if used >= limit:
            return False, f"daily {platform} limit reached ({used}/{limit})"

        global_limit = int(self.rate_limits.get(GLOBAL_KEY, 0) or 0)
        if global_limit > 0:
            used_all = self._counters.used(GLOBAL_KEY)
            if used_all >= global_limit:
                return False, f"global daily publish limit reached ({used_all}/{global_limit})"

        publication = item.publication(platform)
        reason = cooldown_reason(
            publication.last_activity_at,
            min_hours_between,
            attempts=publication.attempts,
        )
        if reason:
            return False, reason

        if not item.title.strip():
            return False, "article has no title"
        if not item.body_markdown.strip():
            return False, "article has no body"
        return True, "ok"

    def record_publish(
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
    ) -> ContentItem:
        """Record one publish attempt; the caller must :meth:`save` straight after.

        Three success shapes, distinguished because they mean different things to
        the ledger:

        * **live** - the article is publicly visible; ``published_at`` and the
          URL are recorded and the item can reach ``published``.
        * **draft** - the platform accepted it but it is not public (DEV's
          ``published: false``, WordPress's ``status: draft``, Hashnode's
          ``createDraft``). Recorded with the URL for reference, but the item is
          *not* counted as live and stays ``approved``.
        * **manual** - handed out for a human to submit; the item becomes
          ``queued`` and only a `confirm()` makes it live.

        Quota is consumed for any non-dry-run attempt, successful or not: a
        rejected request still consumed the platform's attention, and the
        cooldown exists to stop hammering it.
        """
        item = target.item
        platform = target.platform
        publication = item.publication(platform)
        publication.attempts += 1
        publication.mode = mode or publication.mode
        publication.last_attempt_at = now_iso()
        publication.updated_at = now_iso()
        item.attempts += 1
        item.updated_at = now_iso()

        if ok:
            publication.url = url or publication.url
            publication.external_id = external_id or publication.external_id
            publication.last_error = ""
            if mode == MODE_MANUAL:
                publication.status = "manual"
                publication.queued_at = now_iso()
                publication.detail = detail or "prepared for manual submission"
                item.status = CONTENT_QUEUED
            elif live:
                publication.status = "live"
                publication.published_at = now_iso()
                publication.detail = detail or "ok"
                item.last_published_at = now_iso()
                item.status = CONTENT_PUBLISHED
            else:
                publication.status = "draft"
                publication.detail = detail or "saved as a draft, not public"
                publication.published_at = ""
        else:
            publication.status = "failed"
            publication.last_error = (detail or "unknown error")[:500]
            item.last_error = publication.last_error

        item.sync_status()
        if count_against_quota:
            self._counters.bump(platform)
            if self.rate_limits.get(GLOBAL_KEY):
                self._counters.bump(GLOBAL_KEY)
        self._record(
            "publish",
            item=item.id,
            platform=platform,
            mode=publication.mode,
            state=publication.status,
            ok=ok,
            url=publication.url or None,
            error=publication.last_error or None,
        )
        return item

    def record_blocked(self, target: PublishTarget, reason: str) -> ContentItem:
        """Record that the quality gate refused to send this pair.

        Deliberately *not* a publish attempt: nothing was transmitted, so
        `attempts` and the daily counters stay untouched and the platform
        cooldown is not armed. A content problem is an editorial state, not a
        delivery event, and treating it as an attempt would slowly burn a
        platform's daily budget on an article you still have to write.
        """
        item = target.item
        publication = item.publication(target.platform)
        publication.status = "pending"
        publication.detail = (reason or "blocked by the quality gate")[:500]
        publication.updated_at = now_iso()
        item.last_error = publication.detail
        item.updated_at = now_iso()
        self._record(
            "blocked", item=item.id, platform=target.platform, reason=publication.detail
        )
        return item

    def confirm(
        self, item_id: str, platform: str, *, url: str = "", external_id: str = ""
    ) -> ContentItem:
        """Record a human-confirmed manual publication.

        This is the only path from `queued` to `published` for a platform with
        no API, and it refuses anything but an http(s) URL - the ledger exists
        to hold real links.
        """
        key = (platform or "").strip().lower()
        if key not in PLATFORM_IDS:
            raise ContentStoreError(
                f"unknown platform {platform!r}; expected one of {', '.join(PLATFORM_IDS)}"
            )
        if not url.strip().startswith(("http://", "https://")):
            raise ContentStoreError("a live http(s) URL is required to confirm a publication")
        item = self.require(item_id)
        if key not in item.platforms:
            raise ContentStoreError(f"{item.id} does not target {key}")
        publication = item.publication(key)
        publication.status = "live"
        publication.url = url.strip()
        publication.external_id = external_id.strip() or publication.external_id
        publication.published_at = now_iso()
        publication.updated_at = now_iso()
        publication.last_error = ""
        publication.detail = "confirmed by operator"
        if not publication.mode:
            publication.mode = "manual"
        item.last_published_at = now_iso()
        item.updated_at = now_iso()
        item.sync_status()
        self._record("confirm", item=item.id, platform=key, url=publication.url)
        LOG.info("Confirmed %s on %s -> %s", item.id, key, publication.url)
        return item

    def unconfirm(self, item_id: str, platform: str) -> ContentItem:
        """Roll a publication back to pending so it can be re-sent.

        This is a deliberate "send that one again" instruction, so it restores
        the operator's approval rather than demoting the article back to
        `draft` - otherwise the item would drop out of the publish queue and the
        override would appear to do nothing. The cooldown is cleared too, for
        the same reason.
        """
        key = (platform or "").strip().lower()
        item = self.require(item_id)
        if key not in item.platforms:
            raise ContentStoreError(f"{item.id} does not target {key}")
        publication = item.publication(key)
        publication.status = "pending"
        publication.url = ""
        publication.published_at = ""
        publication.queued_at = ""
        publication.last_attempt_at = ""
        publication.attempts = 0
        publication.updated_at = now_iso()
        item.last_published_at = ""
        item.updated_at = now_iso()
        item.sync_status()
        if item.approved_at and item.status not in {CONTENT_PUBLISHED, CONTENT_QUEUED}:
            item.status = CONTENT_APPROVED
        self._record("unconfirm", item=item.id, platform=key)
        LOG.info("Reset %s on %s - it will be offered again", item.id, key)
        return item

    # -- reporting --------------------------------------------------------- #

    def used_today(self, key: str) -> int:
        return self._counters.used(key)

    def daily_usage(self) -> Dict[str, Dict[str, int]]:
        return self._counters.usage(self.rate_limits)

    def summary(self) -> Dict[str, Any]:
        counts = {status: 0 for status in CONTENT_PIPELINE_STATUSES}
        counts[CONTENT_FAILED] = 0
        by_platform: Dict[str, Dict[str, int]] = {}
        live_urls: List[str] = []
        for item in self._items.values():
            counts[item.status] = counts.get(item.status, 0) + 1
            for platform in item.platforms:
                bucket = by_platform.setdefault(platform, {})
                entry = item.publications.get(platform)
                state = "pending"
                if entry is not None:
                    state = entry.status
                bucket[state] = bucket.get(state, 0) + 1
                if entry is not None and entry.is_live:
                    live_urls.append(entry.url)
        return {
            "content_file": str(self.path),
            "total": len(self._items),
            "by_status": counts,
            "by_platform": by_platform,
            "live_urls": live_urls,
            "pending_targets": self.pending_count(),
            "rate_counters": self._counters.to_dict(),
            "daily_usage": self.daily_usage(),
            "recent_history": self._history.recent(8),
        }

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self._history.recent(limit)
