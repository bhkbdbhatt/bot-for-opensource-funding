"""Background job manager for the web UI.

Every long operation (a GitHub discovery sweep, a delivery batch) runs on a
worker thread and reports back through a :class:`Job`. The browser polls
``GET /api/jobs/<id>`` for the live log, progress counters and final result.
This keeps HTTP requests short and lets the UI stay responsive.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

MAX_JOBS = 50
MAX_LOG_LINES = 800


class JobCancelled(Exception):
    """Raised inside a job body when cancellation has been requested."""


@dataclass
class Job:
    """A unit of background work and its observable state."""

    id: str
    kind: str
    label: str
    state: str = "pending"  # pending | running | done | error | cancelled
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    logs: List[str] = field(default_factory=list)
    progress: Dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: str = ""
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- mutation (thread safe) ------------------------------------------- #

    def log(self, message: str) -> None:
        text = str(message).rstrip()
        if not text:
            return
        with self._lock:
            self.logs.append(text)
            if len(self.logs) > MAX_LOG_LINES:
                del self.logs[: len(self.logs) - MAX_LOG_LINES]

    def set_progress(self, **fields: Any) -> None:
        with self._lock:
            self.progress.update(fields)

    def request_cancel(self) -> None:
        self._cancel.set()
        self.log("cancellation requested - will stop at the next safe point")

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- serialization ----------------------------------------------------- #

    def to_dict(self, *, since: int = 0) -> Dict[str, Any]:
        with self._lock:
            logs = self.logs[max(int(since), 0):]
            return {
                "id": self.id,
                "kind": self.kind,
                "label": self.label,
                "state": self.state,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration": (
                    (self.finished_at or time.time()) - (self.started_at or self.created_at)
                ),
                "log_count": len(self.logs),
                "logs": logs,
                "progress": dict(self.progress),
                "result": self.result,
                "error": self.error,
            }


class JobContext:
    """Handed to a job body for logging, progress and cooperative cancel."""

    def __init__(self, job: Job) -> None:
        self.job = job

    def log(self, message: str) -> None:
        self.job.log(message)

    def progress(self, **fields: Any) -> None:
        self.job.set_progress(**fields)

    def check_cancelled(self) -> None:
        if self.job.cancelled:
            raise JobCancelled()


class JobManager:
    """Spawns and tracks background jobs, keeping a bounded history."""

    def __init__(self, *, max_jobs: int = MAX_JOBS) -> None:
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()
        self._max_jobs = max(int(max_jobs), 1)

    def start(self, kind: str, label: str, target: Callable[[JobContext], Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self._max_jobs:
                stale = self._order.pop(0)
                self._jobs.pop(stale, None)
        thread = threading.Thread(
            target=self._run, args=(job, target), name=f"job-{kind}-{job.id}", daemon=True
        )
        thread.start()
        return job

    def _run(self, job: Job, target: Callable[[JobContext], Any]) -> None:
        job.state = "running"
        job.started_at = time.time()
        context = JobContext(job)
        try:
            result = target(context)
            if job.cancelled:
                job.state = "cancelled"
            else:
                job.result = result
                job.state = "done"
        except JobCancelled:
            job.state = "cancelled"
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            job.state = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.log(f"ERROR {job.error}")
        finally:
            job.finished_at = time.time()
            job.set_progress(done=True)

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> List[Job]:
        with self._lock:
            return [self._jobs[job_id] for job_id in reversed(self._order) if job_id in self._jobs]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        job.request_cancel()
        return True
