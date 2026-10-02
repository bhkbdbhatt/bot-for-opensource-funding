"""Channel senders.

Every sender implements the same tiny contract:

    can_send(sponsor) -> (bool, str)   # pre-flight gate
    send(sponsor, message) -> bool     # True on success
"""

from __future__ import annotations

from typing import Tuple

from config_loader import Config
from logging_setup import get_logger
from models import CHANNELS, Sponsor

LOG = get_logger("senders")

__all__ = ["CHANNELS", "Sponsor", "get_sender", "EmailSender", "ForumSender"]


def get_sender(channel: str, config: Config, *, dry_run: bool = False):
    """Factory: return the sender for ``channel``.

    Imported lazily so a broken optional dependency in one channel does not
    take down the other.
    """
    key = (channel or "").strip().lower()
    if key == "email":
        from senders.email_sender import EmailSender

        return EmailSender(config, dry_run=dry_run)
    if key == "forum":
        from senders.forum_sender import ForumSender

        return ForumSender(config, dry_run=dry_run)
    raise ValueError(f"unknown channel {channel!r}; expected one of {', '.join(CHANNELS)}")


def preflight(sponsor: Sponsor) -> Tuple[bool, str]:
    """Channel-agnostic checks."""
    if not sponsor.name:
        return False, "sponsor has no name"
    if not sponsor.channel:
        return False, "sponsor has no channel"
    return True, "ok"