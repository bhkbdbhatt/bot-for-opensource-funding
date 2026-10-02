"""Local web UI for the outreach bot.

The UI is a zero-dependency single-page application served by Python's stdlib
HTTP server. Long-running work (discovery, delivery) runs in background jobs
and the browser polls for progress, so a slow GitHub sweep can never lock the
interface.
"""

from __future__ import annotations

__all__ = ["run_ui"]

from webapp.server import run_ui
