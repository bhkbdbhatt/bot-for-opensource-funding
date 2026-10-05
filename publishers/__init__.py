"""Platform publishers.

Where `senders/` fans one message out to one delivery channel, this package fans
one article out to many syndication targets. The contract is four members rather
than two, because publishing has more to report than sending does:

    check_ready()                     -> (bool, str)   # credential + endpoint present?
    can_publish(item)                 -> (bool, str)   # pre-flight gate
    publish(item, payload)            -> PublishOutcome
    describe() / check()              -> dict          # for `platforms` and the UI

`payload` is the shaped, validated, platform-specific form produced by
`content_engine.prepare_for_platform()` - a publisher never re-reads config to
decide what to send, and never re-implements the quality gate.

Adding a target: create `publishers/<id>.py` with a `BasePublisher` subclass,
add a `PlatformSpec` to `platforms.py`, and list the id in `models.PLATFORM_IDS`.
Nothing else needs to change - the factory, the store's ledger, the CLI and the
web UI all read the registry.

Imports are lazy per platform so a broken credential or endpoint in one target
cannot stop the others from loading, and so `--help` stays fast.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List, Optional

from config_loader import Config
from logging_setup import get_logger
from models import PLATFORM_IDS
from platforms import PLATFORMS, PlatformSpec, require_platform

LOG = get_logger("publishers")

__all__ = [
    "BasePublisher",
    "PublishError",
    "PublishOutcome",
    "PLATFORMS",
    "PLATFORM_IDS",
    "PlatformSpec",
    "build_publisher",
    "check_all",
    "get_publisher",
    "preflight",
]


def get_publisher(platform_id: str, config: Config, *, dry_run: bool = False):
    """Factory: return the publisher for ``platform_id``.

    Raises `ValueError` for an id outside the registry, mirroring
    `senders.get_sender`.
    """
    spec = require_platform(platform_id)
    try:
        module = importlib.import_module(spec.publisher_module)
    except ImportError as exc:  # pragma: no cover - defensive
        raise ValueError(
            f"platform {spec.id!r} could not be loaded ({spec.publisher_module}): {exc}"
        ) from exc
    factory = getattr(module, "build", None)
    if factory is None:
        raise ValueError(
            f"{spec.publisher_module} does not expose build(config, dry_run=...)"
        )
    return factory(config, dry_run=dry_run)


def build_publisher(platform_id: str, config: Config, *, dry_run: bool = False):
    """Alias of :func:`get_publisher` for symmetry with `senders`."""
    return get_publisher(platform_id, config, dry_run=dry_run)


def check_all(
    config: Config, *, dry_run: bool = False, platforms: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """Non-destructive readiness report for every (or the named) platform."""
    ids = [pid for pid in (platforms or PLATFORM_IDS) if pid in PLATFORMS]
    report: List[Dict[str, Any]] = []
    for platform_id in ids:
        try:
            publisher = get_publisher(platform_id, config, dry_run=dry_run)
            report.append(publisher.check())
        except Exception as exc:  # noqa: BLE001 - one bad target must not hide the rest
            LOG.warning("Could not inspect platform %s: %s", platform_id, exc)
            report.append(
                {
                    "platform": platform_id,
                    "label": PLATFORMS[platform_id].label,
                    "ready": False,
                    "enabled": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                    "notes": PLATFORMS[platform_id].notes,
                }
            )
    return report


def preflight(item) -> Optional[str]:  # noqa: ANN001 - avoids importing models
    """Platform-agnostic checks, mirroring `senders.preflight`.

    Returns ``None`` when the article is structurally sound, otherwise a reason.
    """
    if not getattr(item, "title", "").strip():
        return "article has no title"
    if not getattr(item, "body_markdown", "").strip():
        return "article has no body"
    if not getattr(item, "platforms", None):
        return "article has no target platform"
    return None


# Imported last: `publishers.base` imports `config_loader` and `platforms`, and
# exposing them here keeps the public surface readable.
from publishers.base import (  # noqa: E402  isort:skip
    BasePublisher,
    PublishError,
    PublishOutcome,
)
