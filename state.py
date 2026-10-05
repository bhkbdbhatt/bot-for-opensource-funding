"""Shared persistence primitives.

Two independent pipelines write JSON state in this project - the sponsor
outreach ledger (`tracker.py`) and the content syndication ledger
(`content_store.py`). They must obey the *same* two robustness properties, so
those properties live here instead of being copied:

1. **Atomic writes.** A state file is written to a temp file in the same
   directory, flushed, `fsync`ed and then `os.replace`d into place. A crash
   mid-write can never leave a half-written ledger behind.
2. **Self-healing reads.** Unparseable JSON is moved aside to `<name>.corrupt`
   and a fresh document is started, so a damaged file can never wedge the tool.

On top of that this module owns the two small mechanisms both ledgers share: a
date-keyed counter that rolls over at local midnight, and the cooldown gate that
keeps the bot from hammering one target twice in a short window.

Nothing here knows about sponsors or content. It is deliberately generic.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from logging_setup import get_logger
from models import now_iso, today_str

LOG = get_logger("state")

#: Upper bound on the persisted event log, per ledger.
MAX_HISTORY = 500


class StateError(Exception):
    """Raised when a state file cannot be written."""


# --------------------------------------------------------------------------- #
# Atomic JSON document
# --------------------------------------------------------------------------- #


def write_json_atomic(path: str | Path, payload: Any) -> None:
    """Serialise ``payload`` to ``path`` without ever exposing a partial file.

    The temp file is created in the destination directory so `os.replace` stays
    on one filesystem (a cross-device rename is not atomic and would raise).
    """
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, target)
    except OSError as exc:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise StateError(f"cannot write state file {target}: {exc}") from exc


def read_json(path: str | Path, *, quarantine: bool = True) -> Dict[str, Any]:
    """Load a JSON object, quarantining the file if it is unreadable.

    Returns an empty dict when the file is missing. Raises nothing for a corrupt
    document - it is moved aside and the caller starts fresh, which is the
    documented self-healing behaviour of this project.
    """
    target = Path(path).expanduser()
    if not target.is_file():
        return {}
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        LOG.warning("Could not read state file %s: %s", target, exc)
        return {}
    if not raw.strip():
        return {}
    try:
        state = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        if quarantine:
            backup = target.with_suffix(target.suffix + ".corrupt")
            try:
                os.replace(target, backup)
                LOG.error(
                    "State file %s is corrupt (%s). Moved to %s and starting fresh.",
                    target,
                    exc,
                    backup,
                )
            except OSError:
                LOG.error("State file %s is corrupt (%s) and could not be moved.", target, exc)
        else:
            LOG.error("State file %s is corrupt (%s); ignoring it.", target, exc)
        return {}
    if not isinstance(state, dict):
        LOG.error("State root in %s is not an object; ignoring it", target)
        return {}
    return state


class AtomicJsonStore:
    """A single JSON document with atomic persistence and bounded history.

    Subclasses (or owners) supply the document body through :meth:`_document`
    and call :meth:`save` after every mutation that matters.
    """

    #: Written into the document so a future format change can be detected.
    schema_version: int = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        #: Guards read-modify-write cycles. `ThreadingHTTPServer` runs each UI
        #: request on its own thread, so background jobs can overlap.
        self.lock = threading.RLock()

    # -- to be provided by the owner ------------------------------------- #

    def _document(self) -> Dict[str, Any]:
        raise NotImplementedError

    # -- persistence ------------------------------------------------------- #

    def save(self) -> None:
        """Atomically persist the current document."""
        with self.lock:
            payload = self._document()
            payload.setdefault("version", self.schema_version)
            payload["updated_at"] = now_iso()
            try:
                write_json_atomic(self.path, payload)
            except StateError as exc:
                LOG.error("%s", exc)
                raise

    def load(self) -> Dict[str, Any]:
        return read_json(self.path)

    @property
    def exists(self) -> bool:
        return self.path.is_file()


# --------------------------------------------------------------------------- #
# Daily counters
# --------------------------------------------------------------------------- #


class DailyCounters:
    """Per-target send/publish counters that reset at local midnight.

    The whole document lives in memory and is written back by the owning store,
    which is what keeps rate limiting honest across process restarts.
    """

    def __init__(
        self,
        raw: Optional[Dict[str, Any]] = None,
        keys: Optional[Iterable[str]] = None,
    ) -> None:
        self._keys = [str(key) for key in (keys or [])]
        self._date = str((raw or {}).get("date") or today_str())
        self._values: Dict[str, int] = {
            key: _safe_int((raw or {}).get(key)) for key in self._keys
        }

    # -- keys -------------------------------------------------------------- #

    @property
    def keys(self) -> List[str]:
        return list(self._keys)

    def ensure_key(self, key: str, limit: int = 0) -> None:
        """Register a target discovered after load (config changed, new platform)."""
        key = str(key)
        if key not in self._values:
            self._keys.append(key)
            self._values[key] = 0
        _ = limit

    # -- access ------------------------------------------------------------ #

    def used(self, key: str) -> int:
        self.roll()
        return int(self._values.get(str(key), 0))

    def bump(self, key: str, amount: int = 1) -> int:
        self.roll()
        key = str(key)
        self.ensure_key(key)
        self._values[key] = int(self._values.get(key, 0)) + int(amount)
        return self._values[key]

    def roll(self) -> bool:
        """Reset every counter when the local calendar day has changed."""
        today = today_str()
        if self._date == today:
            return False
        LOG.info("Rate counters reset for %s", today)
        self._date = today
        self._values = {key: 0 for key in self._keys}
        return True

    def usage(self, limits: Dict[str, int]) -> Dict[str, Dict[str, int]]:
        self.roll()
        result: Dict[str, Dict[str, int]] = {}
        for key in self._keys:
            limit = max(int(limits.get(key, 0) or 0), 0)
            used = int(self._values.get(key, 0))
            result[key] = {"used": used, "limit": limit, "remaining": max(limit - used, 0)}
        return result

    def to_dict(self) -> Dict[str, Any]:
        self.roll()
        document: Dict[str, Any] = {"date": self._date}
        document.update({key: int(self._values.get(key, 0)) for key in self._keys})
        return document


# --------------------------------------------------------------------------- #
# Cooldown gate
# --------------------------------------------------------------------------- #


def cooldown_reason(
    last_at: str,
    min_hours: int,
    *,
    attempts: int = 1,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Return why a target is still cooling down, or ``None`` when it is ready.

    ``attempts`` guards against the degenerate case where a ledger entry carries
    a timestamp but no attempts were ever made - that entry must not be able to
    arm the cooldown against itself.
    """
    hours = int(min_hours or 0)
    if hours <= 0 or int(attempts or 0) <= 0 or not last_at:
        return None
    try:
        last = datetime.fromisoformat(str(last_at))
    except ValueError:
        return None
    reference = now or datetime.now(last.tzinfo)
    elapsed = reference - last
    if elapsed >= timedelta(hours=hours):
        return None
    remaining = int((timedelta(hours=hours) - elapsed).total_seconds() // 60)
    return f"cooling down ({remaining} min since last attempt)"


# --------------------------------------------------------------------------- #
# Bounded event log
# --------------------------------------------------------------------------- #


class BoundedLog:
    """A capped list of event dicts, newest last."""

    def __init__(self, raw: Optional[List[Any]] = None, *, limit: int = MAX_HISTORY) -> None:
        self._limit = max(int(limit), 1)
        self._entries: List[Dict[str, Any]] = [
            dict(entry) for entry in (raw or []) if isinstance(entry, dict)
        ][-self._limit:]

    def append(self, event: str, **fields: Any) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"at": now_iso(), "event": event}
        entry.update(fields)
        self._entries.append(entry)
        if len(self._entries) > self._limit:
            del self._entries[: len(self._entries) - self._limit]
        return entry

    def to_list(self) -> List[Dict[str, Any]]:
        return [dict(entry) for entry in self._entries]

    def recent(self, limit: int = 8) -> List[Dict[str, Any]]:
        return self.to_list()[-max(int(limit), 0):]

    def __len__(self) -> int:
        return len(self._entries)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
