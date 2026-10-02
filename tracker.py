"""JSON-backed sponsor tracker.

Pipeline:  new -> contacted -> replied -> sponsored
(`failed` is a bookkeeping state used when delivery errors out.)

The tracker file is written atomically (temp file + os.replace) so a crash
mid-write can never corrupt state. Daily rate-limit counters live in the same
file, which keeps rate limiting honest across process restarts.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
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
    today_str,
)

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


class SponsorTracker:
    """In-memory view over the JSON tracker file."""

    def __init__(
        self,
        path: str | Path,
        *,
        seed_sponsors: Optional[Iterable[Dict[str, Any]]] = None,
        rate_limits: Optional[Dict[str, int]] = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.rate_limits = dict(rate_limits or {CHANNEL_EMAIL: 15, CHANNEL_FORUM: 3})
        self._sponsors: Dict[str, Sponsor] = {}
        self._seen: Dict[str, Dict[str, Any]] = {}
        self._history: List[Dict[str, Any]] = []
        self._counters: Dict[str, Any] = {"date": today_str(), CHANNEL_EMAIL: 0, CHANNEL_FORUM: 0}
        self._load()
        if not self.path.exists():
            self._seed(seed_sponsors or [])
            self.save()

    # -- persistence ------------------------------------------------------- #

    def _empty_state(self) -> Dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "sponsors": [],
            "seen": {},
            "rate_counters": {"date": today_str(), CHANNEL_EMAIL: 0, CHANNEL_FORUM: 0},
            "history": [],
        }

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            state = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            try:
                self.path.replace(backup)
                LOG.error(
                    "Tracker file is corrupt (%s). Moved to %s and starting fresh.",
                    exc,
                    backup,
                )
            except OSError:
                LOG.error("Tracker file is corrupt (%s) and could not be moved.", exc)
            return
        except OSError as exc:
            LOG.warning("Could not read tracker %s: %s", self.path, exc)
            return

        if not isinstance(state, dict):
            LOG.error("Tracker root is not an object; ignoring %s", self.path)
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
            self._history = [entry for entry in history if isinstance(entry, dict)]
        counters = state.get("rate_counters")
        if isinstance(counters, dict):
            self._counters = {
                "date": str(counters.get("date") or today_str()),
                CHANNEL_EMAIL: int(counters.get(CHANNEL_EMAIL) or 0),
                CHANNEL_FORUM: int(counters.get(CHANNEL_FORUM) or 0),
            }
        self._roll_counters()

    def _state(self) -> Dict[str, Any]:
        state = self._empty_state()
        state.update(
            {
                "sponsors": [self._sponsors[key].to_dict() for key in sorted(self._sponsors)],
                "seen": self._seen,
                "rate_counters": self._counters,
                "history": self._history[-MAX_HISTORY:],
                "updated_at": now_iso(),
            }
        )
        return state

    def save(self) -> None:
        """Atomically persist state."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._state(), indent=2, ensure_ascii=False)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except OSError as exc:
            LOG.error("Failed to persist tracker %s: %s", self.path, exc)
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise TrackerError(f"cannot write tracker file {self.path}: {exc}") from exc

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
        entry = {"at": now_iso(), "event": event}
        entry.update(fields)
        self._history.append(entry)
        if len(self._history) > MAX_HISTORY:
            self._history = self._history[-MAX_HISTORY:]

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

        if min_hours_between_attempts > 0 and sponsor.attempts and sponsor.last_contacted_at:
            try:
                last = datetime.fromisoformat(sponsor.last_contacted_at)
            except ValueError:
                last = datetime.now()
            elapsed = datetime.now(last.tzinfo) - last
            if elapsed < timedelta(hours=min_hours_between_attempts):
                remaining = int(
                    (timedelta(hours=min_hours_between_attempts) - elapsed).total_seconds() // 60
                )
                return False, f"cooling down ({remaining} min since last attempt)"
        return True, "ok"

    def record_send(self, name: str, channel: str, *, ok: bool, error: str = "") -> Sponsor:
        sponsor = self.require(name)
        sponsor.attempts += 1
        sponsor.updated_at = now_iso()
        if ok:
            self._bump_counter(channel)
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

    def _roll_counters(self) -> bool:
        today = today_str()
        if self._counters.get("date") == today:
            return False
        LOG.info("Rate counters reset for %s", today)
        self._counters = {"date": today, CHANNEL_EMAIL: 0, CHANNEL_FORUM: 0}
        return True

    def _bump_counter(self, channel: str) -> None:
        self._roll_counters()
        self._counters[channel] = int(self._counters.get(channel, 0)) + 1

    def used_today(self, channel: str) -> int:
        self._roll_counters()
        return int(self._counters.get(channel, 0))

    def daily_usage(self) -> Dict[str, Dict[str, Any]]:
        self._roll_counters()
        return {
            channel: {
                "used": int(self._counters.get(channel, 0)),
                "limit": int(self.rate_limits.get(channel, 0) or 0),
                "remaining": max(int(self.rate_limits.get(channel, 0) or 0) - int(self._counters.get(channel, 0)), 0),
            }
            for channel in CHANNELS
        }

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
            "rate_counters": dict(self._counters),
            "daily_usage": self.daily_usage(),
            "recent_history": self._history[-8:],
        }

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self._history[-max(int(limit), 0):]