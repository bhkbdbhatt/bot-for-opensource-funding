"""JSON-backed sponsor tracker.

Pipeline:  new -> contacted -> replied -> sponsored
(`failed` is a bookkeeping state used when delivery errors out.)

The tracker file is written atomically (temp file + os.replace) so a crash
mid-write can never corrupt state. Daily rate-limit counters live in the same
file, which keeps rate limiting honest across process restarts.

Persistence, the daily counters, the cooldown gate and the bounded event log are
all provided by `state.py`, which `content_store.py` shares - the two pipelines
must not drift apart on how they keep state safe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from logging_setup import get_logger
from models import (
    CHANNEL_EMAIL,
    CHANNEL_FORUM,
    CHANNELS,
    PIPELINE_STATUSES,
    STATUS_CONTACTED,
    STATUS_NEW,
    STATUSES,
    DiscoveredSponsor,
    Sponsor,
    normalize_name,
    now_iso,
)
from state import AtomicJsonStore, BoundedLog, DailyCounters, StateError, cooldown_reason

LOG = get_logger("tracker")

SCHEMA_VERSION = 1
MAX_HISTORY = 500


class TrackerError(Exception):
    """Raised for unusable tracker state."""


class SponsorNotFound(TrackerError):
    def __init__(self, name: str) -> None:
        super().__init__(f"sponsor not found: {name!r}")
        self.name = name


# --------------------------------------------------------------------------- #
# Tracker
# --------------------------------------------------------------------------- #


class SponsorTracker(AtomicJsonStore):
    """In-memory view over the JSON tracker file."""

    schema_version = SCHEMA_VERSION

    def __init__(
        self,
        path: str | Path,
        *,
        seed_sponsors: Optional[Iterable[Dict[str, Any]]] = None,
        rate_limits: Optional[Dict[str, int]] = None,
    ) -> None:
        super().__init__(path)
        self.rate_limits = dict(rate_limits or {CHANNEL_EMAIL: 15, CHANNEL_FORUM: 3})
        self._sponsors: Dict[str, Sponsor] = {}
        self._seen: Dict[str, Dict[str, Any]] = {}
        self._history = BoundedLog([], limit=MAX_HISTORY)
        self._counters = DailyCounters(keys=list(CHANNELS))
        self._load()
        if not self.path.exists():
            self._seed(seed_sponsors or [])
            self.save()

    # -- persistence ------------------------------------------------------- #

    def _document(self) -> Dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "sponsors": [self._sponsors[key].to_dict() for key in sorted(self._sponsors)],
            "seen": self._seen,
            "rate_counters": self._counters.to_dict(),
            "history": self._history.to_list(),
        }

    def _load(self) -> None:
        state = self.load()
        if not state:
            return

        for raw in state.get("sponsors") or []:
            try:
                sponsor = Sponsor.from_dict(raw)
            except (ValueError, TypeError) as exc:
                LOG.error("Skipping malformed sponsor entry: %s", exc)
                continue
            if not sponsor.name:
                continue
            # Self-healing migration: you cannot have been contacted if no
            # send was ever attempted. Older tracker files (and older versions
            # of Sponsor.from_dict) stamped a synthetic last_contacted_at on
            # every entry, which would otherwise arm the cooldown timer.
            if not sponsor.attempts:
                sponsor.last_contacted_at = ""
            self._sponsors[sponsor.key] = sponsor

        seen = state.get("seen")
        if isinstance(seen, dict):
            self._seen = seen
        history = state.get("history")
        if isinstance(history, list):
            self._history = BoundedLog(history, limit=MAX_HISTORY)
        counters = state.get("rate_counters")
        if isinstance(counters, dict):
            self._counters = DailyCounters(counters, keys=list(CHANNELS))
        self._counters.roll()

    def save(self) -> None:
        """Atomically persist state."""
        try:
            super().save()
        except StateError as exc:
            raise TrackerError(str(exc)) from exc

    def _seed(self, entries: Iterable[Dict[str, Any]]) -> None:
        count = 0
        for raw in entries:
            try:
                sponsor = Sponsor.from_dict(raw)
            except (ValueError, TypeError) as exc:
                LOG.error("Skipping malformed seed sponsor: %s", exc)
                continue
            if sponsor.validate():
                LOG.error(
                    "Skipping seed sponsor %r: %s", sponsor.name, "; ".join(sponsor.validate())
                )
                continue
            sponsor.source = "config" if sponsor.source == "manual" else sponsor.source
            if sponsor.key not in self._sponsors:
                self._sponsors[sponsor.key] = sponsor
                count += 1
        if count:
            LOG.info("Seeded %d sponsor(s) from config", count)

    def _record(self, event: str, **fields: Any) -> None:
        self._history.append(event, **fields)

    # -- CRUD -------------------------------------------------------------- #

    def add(self, sponsor: Sponsor, *, require_email: bool = False) -> tuple[Sponsor, bool]:
        """Insert a sponsor. Returns ``(sponsor, created)``.

        Existing entries are enriched (never overwritten destructively) so that
        re-running discovery cannot clobber pipeline state.
        """
        errors = sponsor.validate(require_email=require_email)
        if errors:
            raise TrackerError("; ".join(errors))

        existing = self._sponsors.get(sponsor.key)
        if existing is None:
            self._sponsors[sponsor.key] = sponsor
            self._record("added", sponsor=sponsor.name, channel=sponsor.channel)
            LOG.info("Added sponsor %s (%s/%s)", sponsor.name, sponsor.channel, sponsor.status)
            return sponsor, True

        changed = False
        for attr in ("email", "contact", "company", "github", "website", "notes", "stars", "owner_type"):
            new_value = getattr(sponsor, attr)
            if new_value and not getattr(existing, attr):
                setattr(existing, attr, new_value)
                changed = True
        if changed:
            existing.updated_at = now_iso()
            self._record("updated", sponsor=existing.name)
            LOG.info("Enriched existing sponsor %s", existing.name)
        else:
            LOG.info("Sponsor %s already tracked (no changes)", existing.name)
        return existing, False

    def get(self, name: str) -> Optional[Sponsor]:
        return self._sponsors.get(normalize_name(name))

    def require(self, name: str) -> Sponsor:
        sponsor = self.get(name)
        if sponsor is None:
            raise SponsorNotFound(name)
        return sponsor

    def remove(self, name: str) -> Sponsor:
        sponsor = self.require(name)
        del self._sponsors[sponsor.key]
        self._record("removed", sponsor=sponsor.name)
        LOG.info("Removed sponsor %s", sponsor.name)
        return sponsor

    def all(self, *, statuses: Optional[Iterable[str]] = None, channel: Optional[str] = None) -> List[Sponsor]:
        wanted = {status.lower() for status in statuses} if statuses else None
        items = list(self._sponsors.values())
        if wanted:
            items = [item for item in items if item.status in wanted]
        if channel:
            items = [item for item in items if item.channel == channel]
        return sorted(items, key=lambda item: item.rank())

    # -- pipeline ---------------------------------------------------------- #

    def update_status(self, name: str, status: str, *, note: str = "") -> Sponsor:
        status = (status or "").strip().lower()
        if status not in STATUSES:
            raise TrackerError(
                f"invalid status {status!r}; expected one of {', '.join(STATUSES)}"
            )
        sponsor = self.require(name)
        previous = sponsor.status
        sponsor.status = status
        sponsor.updated_at = now_iso()
        if status == STATUS_CONTACTED and not sponsor.last_contacted_at:
            sponsor.last_contacted_at = now_iso()
        if status not in {STATUS_CONTACTED}:
            sponsor.last_error = ""
        self._record("status", sponsor=sponsor.name, **{"from": previous, "to": status})
        LOG.info("Status %s: %s -> %s%s", sponsor.name, previous, status, f" ({note})" if note else "")
        return sponsor

    def next_batch(self, n: int = 5, *, channels: Optional[Iterable[str]] = None) -> List[Sponsor]:
        """Up to ``n`` sponsors still in ``new``, best first."""
        allowed = {channel.lower() for channel in channels} if channels else set(CHANNELS)
        candidates = [
            sponsor
            for sponsor in self._sponsors.values()
            if sponsor.status == STATUS_NEW and sponsor.channel in allowed
        ]
        return sorted(candidates, key=lambda item: item.rank())[: max(int(n), 0)]

    # -- delivery bookkeeping ---------------------------------------------- #

    def can_send(self, sponsor: Sponsor, *, min_hours_between_attempts: int = 0) -> tuple[bool, str]:
        """Rate-limit gate. Returns ``(allowed, reason)``."""
        channel = sponsor.channel
        if channel not in CHANNELS:
            return False, f"unknown channel {channel!r}"

        limit = int(self.rate_limits.get(channel, 0) or 0)
        used = self.used_today(channel)
        if used >= limit:
            return False, f"daily {channel} limit reached ({used}/{limit})"

        reason = cooldown_reason(
            sponsor.last_contacted_at,
            min_hours_between_attempts,
            attempts=sponsor.attempts,
        )
        if reason:
            return False, reason
        return True, "ok"

    def record_send(self, name: str, channel: str, *, ok: bool, error: str = "") -> Sponsor:
        sponsor = self.require(name)
        sponsor.attempts += 1
        sponsor.updated_at = now_iso()
        if ok:
            self._counters.bump(channel)
            if sponsor.status == STATUS_NEW:
                sponsor.status = STATUS_CONTACTED
            sponsor.last_contacted_at = now_iso()
            sponsor.last_error = ""
        else:
            sponsor.last_error = (error or "unknown error")[:500]
        self._record(
            "send",
            sponsor=sponsor.name,
            channel=channel,
            ok=ok,
            error=sponsor.last_error or None,
        )
        return sponsor

    def used_today(self, channel: str) -> int:
        return self._counters.used(channel)

    def daily_usage(self) -> Dict[str, Dict[str, Any]]:
        return self._counters.usage(self.rate_limits)

    # -- discovery dedupe -------------------------------------------------- #

    def is_seen(self, owner_name: str) -> bool:
        return normalize_name(owner_name) in self._seen

    def seen_entry(self, owner_name: str) -> Optional[Dict[str, Any]]:
        return self._seen.get(normalize_name(owner_name))

    def mark_seen(self, discovered: DiscoveredSponsor) -> None:
        self._seen[discovered.key] = {
            "owner_name": discovered.owner_name,
            "owner_type": discovered.owner_type,
            "github_url": discovered.github_url,
            "stars": discovered.stars,
            "genesys_repos_count": discovered.genesys_repos_count,
            "email_if_public": discovered.email_if_public,
            "top_repo": discovered.top_repo,
            "discovered_from": discovered.discovered_from,
            "first_seen_at": discovered.discovered_at,
            "last_seen_at": now_iso(),
            "sponsor_status": (
                self._sponsors[discovered.key].status
                if discovered.key in self._sponsors
                else None
            ),
        }

    def seen_count(self) -> int:
        return len(self._seen)

    # -- reporting --------------------------------------------------------- #

    def summary(self) -> Dict[str, Any]:
        counts = {status: 0 for status in PIPELINE_STATUSES}
        by_channel: Dict[str, Dict[str, int]] = {channel: {} for channel in CHANNELS}
        for sponsor in self._sponsors.values():
            if sponsor.status in counts:
                counts[sponsor.status] += 1
            bucket = by_channel.setdefault(sponsor.channel, {})
            bucket[sponsor.status] = bucket.get(sponsor.status, 0) + 1
        return {
            "tracker_file": str(self.path),
            "total": len(self._sponsors),
            "by_status": counts,
            "by_channel": by_channel,
            "failures": sum(1 for s in self._sponsors.values() if s.status == "failed"),
            "discovered_seen": self.seen_count(),
            "rate_counters": self._counters.to_dict(),
            "daily_usage": self.daily_usage(),
            "recent_history": self._history.recent(8),
        }

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self._history.recent(limit)