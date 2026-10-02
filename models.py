"""Shared data models, status constants and validation helpers.

Everything the bot persists or passes between modules is defined here so that
discovery, tracker, prompt engine and senders agree on shapes.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Dict, List

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

STATUS_NEW = "new"
STATUS_CONTACTED = "contacted"
STATUS_REPLIED = "replied"
STATUS_SPONSORED = "sponsored"
STATUS_FAILED = "failed"

#: The sponsorship pipeline, in order.
PIPELINE_STATUSES = (STATUS_NEW, STATUS_CONTACTED, STATUS_REPLIED, STATUS_SPONSORED)

#: Every status the tracker accepts. ``failed`` is a bookkeeping state for
#: delivery errors; it is not part of the pipeline and can be moved back to
#: ``new`` at any time.
STATUSES = PIPELINE_STATUSES + (STATUS_FAILED,)

CHANNEL_EMAIL = "email"
CHANNEL_FORUM = "forum"
CHANNELS = (CHANNEL_EMAIL, CHANNEL_FORUM)

OWNER_ORG = "Organization"
OWNER_USER = "User"

_EMAIL_RE = re.compile(r"^[^@\s;,]+@[^@\s;,]+\.[A-Za-z]{2,}$")
_MAX_EMAIL_LEN = 254
_MAX_URL_LEN = 2048

#: Domain fragments that must never be treated as a real sponsor contact.
EMAIL_NOISE_SUBSTRINGS = (
    "example.com",
    "example.org",
    "example.net",
    "yourdomain",
    "mydomain",
    "domain.com",
    "email.com",
    "sentry.io",
    "wixpress.com",
    "godaddy.com",
    "squarespace",
    "placeholder",
    "template",
    "noreply",
    "no-reply",
    "donotreply",
    "webmaster@",
    "postmaster@",
    "abuse@",
    "spam@",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def now_iso() -> str:
    """Local-time ISO-8601 timestamp, second precision."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def today_str() -> str:
    """Local calendar date as ``YYYY-MM-DD`` (used for daily rate counters)."""
    return datetime.now().strftime("%Y-%m-%d")


def normalize_name(value: str) -> str:
    """Case/whitespace-insensitive key for a sponsor name."""
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def is_valid_email(value: str) -> bool:
    candidate = (value or "").strip()
    if not candidate or len(candidate) > _MAX_EMAIL_LEN:
        return False
    return bool(_EMAIL_RE.match(candidate))


def is_valid_url(value: str) -> bool:
    candidate = (value or "").strip()
    if not candidate or len(candidate) > _MAX_URL_LEN:
        return False
    return candidate.startswith(("http://", "https://"))


def looks_like_noise_email(value: str) -> bool:
    lowered = (value or "").strip().lower()
    return any(fragment in lowered for fragment in EMAIL_NOISE_SUBSTRINGS)


def word_count(text: str) -> int:
    return len([token for token in re.split(r"\s+", (text or "").strip()) if token])


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        return [part for part in parts if part]
    if isinstance(value, (list, tuple, set)):
        return [_as_str(item) for item in value if _as_str(item)]
    return []


# --------------------------------------------------------------------------- #
# Sponsor
# --------------------------------------------------------------------------- #


@dataclass
class Sponsor:
    """A single potential sponsor moving through the pipeline."""

    name: str
    email: str = ""
    channel: str = CHANNEL_EMAIL
    status: str = STATUS_NEW
    contact: str = ""          # forum handle / Slack / contact form URL
    company: str = ""
    github: str = ""           # GitHub login, used for email enrichment
    website: str = ""
    notes: str = ""
    source: str = "manual"     # manual | config | discovery
    stars: int = 0
    owner_type: str = ""
    priority: int = 0
    attempts: int = 0
    last_error: str = ""
    added_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    last_contacted_at: str = ""

    # -- serialization ----------------------------------------------------- #

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Sponsor":
        """Build a Sponsor from a dict, ignoring unknown keys."""
        if not isinstance(raw, dict):
            raise ValueError("sponsor entry must be a mapping")
        known = {f.name for f in fields(cls)}
        data = {key: value for key, value in raw.items() if key in known}
        data["name"] = _as_str(data.get("name"))
        data["email"] = _as_str(data.get("email"))
        data["channel"] = _as_str(data.get("channel"), CHANNEL_EMAIL).lower()
        data["status"] = _as_str(data.get("status"), STATUS_NEW).lower()
        data["contact"] = _as_str(data.get("contact"))
        data["company"] = _as_str(data.get("company"))
        data["github"] = _as_str(data.get("github")).lstrip("@")
        data["website"] = _as_str(data.get("website"))
        data["notes"] = _as_str(data.get("notes"))
        data["source"] = _as_str(data.get("source"), "manual")
        data["owner_type"] = _as_str(data.get("owner_type"))
        data["last_error"] = _as_str(data.get("last_error"))
        data["stars"] = _as_int(data.get("stars"))
        data["priority"] = _as_int(data.get("priority"))
        data["attempts"] = _as_int(data.get("attempts"))
        for stamp in ("added_at", "updated_at"):
            data[stamp] = _as_str(data.get(stamp)) or now_iso()
        # `last_contacted_at` means "when we last transmitted to them", so an
        # absent value must stay empty. Defaulting it to now would make every
        # freshly loaded sponsor look like it was contacted this second and
        # would arm the min_hours_between_attempts cooldown against itself.
        data["last_contacted_at"] = _as_str(data.get("last_contacted_at"))
        sponsor = cls(**data)
        # Keep stored email addresses canonical (trailing commas etc. are a
        # very common copy/paste artefact from directories).
        if sponsor.email:
            sponsor.email = sponsor.email.strip().strip(",;<>").strip().lower()
        return sponsor

    # -- validation -------------------------------------------------------- #

    def validate(self, *, require_email: bool = False) -> List[str]:
        """Return a list of human readable problems (empty list == valid)."""
        errors: List[str] = []
        if not self.name:
            errors.append("name is required")
        if len(self.name) > 120:
            errors.append("name must be 120 characters or fewer")
        if self.channel not in CHANNELS:
            errors.append(f"channel must be one of {', '.join(CHANNELS)} (got {self.channel!r})")
        if self.status not in STATUSES:
            errors.append(f"status must be one of {', '.join(STATUSES)} (got {self.status!r})")
        if self.email and not is_valid_email(self.email):
            errors.append(f"email {self.email!r} is not a valid address")
        if self.email and self.email != self.email.lower():
            errors.append("email must be lowercase")
        if require_email and not self.email and self.channel == CHANNEL_EMAIL:
            errors.append("email is required for the 'email' channel (or use --no-email)")
        if self.website and not is_valid_url(self.website):
            errors.append(f"website {self.website!r} must start with http:// or https://")
        return errors

    # -- behaviour --------------------------------------------------------- #

    @property
    def key(self) -> str:
        return normalize_name(self.name)

    def target(self) -> str:
        """Best available destination for this sponsor."""
        if self.channel == CHANNEL_FORUM:
            return self.contact or self.website or self.name
        return self.email

    def rank(self) -> tuple:
        """Sort key for `next_batch`: priority, then stars, then age."""
        return (-self.priority, -self.stars, self.added_at, self.name.lower())

    def summary_line(self) -> str:
        destination = self.target() or "-"
        return (
            f"{self.name} | {self.channel} | {self.status} | "
            f"stars={self.stars} | {destination}"
        )


# --------------------------------------------------------------------------- #
# DiscoveredSponsor
# --------------------------------------------------------------------------- #


@dataclass
class DiscoveredSponsor:
    """A candidate found by scanning GitHub topic repositories."""

    owner_name: str
    owner_type: str = OWNER_USER
    email_if_public: str = ""
    github_url: str = ""
    repos_count: int = 0                 # total public repos on the account
    genesys_repos_count: int = 0         # repos tagged with the scraped topics
    top_repo: str = ""
    top_repo_url: str = ""
    stars: int = 0
    forks: int = 0
    description: str = ""
    language: str = ""
    website: str = ""
    blog: str = ""
    location: str = ""
    topics: List[str] = field(default_factory=list)
    discovered_from: List[str] = field(default_factory=list)
    discovered_at: str = field(default_factory=now_iso)
    email_source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "DiscoveredSponsor":
        if not isinstance(raw, dict):
            raise ValueError("discovery entry must be a mapping")
        known = {f.name for f in fields(cls)}
        data = {key: value for key, value in raw.items() if key in known}
        data["owner_name"] = _as_str(data.get("owner_name"))
        for numeric in (
            "repos_count",
            "genesys_repos_count",
            "stars",
            "forks",
        ):
            data[numeric] = _as_int(data.get(numeric))
        for text in (
            "owner_type",
            "email_if_public",
            "github_url",
            "top_repo",
            "top_repo_url",
            "description",
            "language",
            "website",
            "blog",
            "location",
            "email_source",
            "discovered_at",
        ):
            data[text] = _as_str(data.get(text))
        for lst in ("topics", "discovered_from"):
            data[lst] = _as_str_list(data.get(lst))
        return cls(**data)

    @property
    def key(self) -> str:
        return normalize_name(self.owner_name)

    @property
    def is_org(self) -> bool:
        return self.owner_type.lower().startswith("org")

    def to_sponsor(self, channel: str = CHANNEL_EMAIL, notes: str = "") -> Sponsor:
        """Project a discovered candidate onto a trackable Sponsor."""
        if not notes:
            notes = (
                f"Discovered via topics: {', '.join(self.discovered_from) or 'n/a'}"
            )
            if self.top_repo:
                notes += f" | top repo: {self.top_repo}"
            if self.email_source:
                notes += f" | email via {self.email_source}"
        return Sponsor(
            name=self.owner_name,
            email=self.email_if_public,
            channel=channel,
            status=STATUS_NEW,
            company=self.owner_name if self.is_org else "",
            github=self.owner_name,
            website=self.website or self.blog,
            notes=notes,
            source="discovery",
            stars=self.stars,
            owner_type=self.owner_type,
            priority=min(10, self.stars // 50),
        )

    def summary_line(self) -> str:
        return (
            f"{self.owner_name} ({self.owner_type}) | repos={self.repos_count} "
            f"genesys={self.genesys_repos_count} | stars={self.stars} "
            f"| {self.email_if_public or 'no public email'} | {self.top_repo}"
        )