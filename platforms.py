"""Registry of syndication targets ("platforms").

One `PlatformSpec` per target, describing *what it can do* rather than *how it
is configured*. Config (keys, endpoints, credentials) lives in `config.yaml`;
this module owns the immutable facts a publisher needs to decide whether it can
run at all and how to shape a payload:

* `kind` - whether a public publishing API exists at all. This is the single
  most important field: two of the requested communities (CoderLegion, DevDojo)
  publish no API and only accept submissions through a signed-in web form, so
  the honest implementation is a hand-off, not a fabricated endpoint.
* `content_format` - markdown vs HTML, which decides whether the payload passes
  through or goes via the markdown renderer first.
* `tag_limit`, `max_title_words`, `max_body_bytes`, `supports_canonical`,
  `supports_draft` - the platform's own constraints, enforced centrally so no
  publisher invents its own numbers.
* `auth` - how a credential is supplied (env var name, header shape or user +
  application password), and `token_help` - the URL where a human obtains one.

`publisher_module` is the lazy-import target, keeping `promoter.py` free of any
import-time dependency on `requests`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from models import PLATFORM_IDS

# --------------------------------------------------------------------------- #
# Capability record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlatformSpec:
    """Immutable capability record for one syndication target."""

    id: str
    label: str
    kind: str                      # api | manual | webhook
    publisher_module: str
    content_format: str = "markdown"       # markdown | html
    auth: str = "api_key"                  # api_key | bearer | basic | none | url_env
    token_env: str = ""
    docs_url: str = ""
    token_help: str = ""
    submission_url: str = ""
    tag_limit: int = 5
    max_title_words: int = 12
    max_title_chars: int = 128
    max_body_bytes: int = 100_000
    min_body_words: int = 0
    supports_canonical: bool = False
    supports_tags: bool = True
    supports_draft: bool = False
    supports_categories: bool = False
    #: Default daily publish ceiling, overridable per campaign in `rate_limits`.
    default_daily_limit: int = 1
    #: Seconds to wait after a successful publish on this platform.
    cooldown_hours: int = 24
    notes: str = ""
    #: Platforms that do not accept new API credentials any more. Kept
    #: functional, disabled by default, and surfaced in `platforms` output.
    legacy: bool = False
    extra: Dict[str, str] = field(default_factory=dict)

    # -- behaviour --------------------------------------------------------- #

    @property
    def accepts_markdown(self) -> bool:
        return self.content_format == "markdown"

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "content_format": self.content_format,
            "auth": self.auth,
            "token_env": self.token_env,
            "docs_url": self.docs_url,
            "token_help": self.token_help,
            "submission_url": self.submission_url,
            "tag_limit": self.tag_limit,
            "max_title_words": self.max_title_words,
            "max_title_chars": self.max_title_chars,
            "max_body_bytes": self.max_body_bytes,
            "min_body_words": self.min_body_words,
            "supports_canonical": self.supports_canonical,
            "supports_tags": self.supports_tags,
            "supports_draft": self.supports_draft,
            "supports_categories": self.supports_categories,
            "default_daily_limit": self.default_daily_limit,
            "cooldown_hours": self.cooldown_hours,
            "notes": self.notes,
            "legacy": self.legacy,
        }


# --------------------------------------------------------------------------- #
# The registry
#
# Endpoint/credential defaults for every platform live here too, because a
# publisher must be able to run with nothing but a bare `platforms.<id>.enabled`
# flag. All of them are overridable in config.yaml.
# --------------------------------------------------------------------------- #

PLATFORMS: Dict[str, PlatformSpec] = {
    "devto": PlatformSpec(
        id="devto",
        label="DEV Community (dev.to)",
        kind="api",
        publisher_module="publishers.devto",
        content_format="markdown",
        auth="api_key",
        token_env="DEVTO_API_KEY",
        docs_url="https://developers.forem.com/api",
        token_help="https://dev.to/settings/extensions",
        tag_limit=4,
        max_title_words=12,
        max_title_chars=128,
        max_body_bytes=100_000,
        min_body_words=120,
        supports_canonical=True,
        supports_draft=True,
        supports_categories=True,
        default_daily_limit=1,
        cooldown_hours=24,
        notes=(
            "Forem v1 API. Needs the api-key header plus "
            "accept: application/vnd.forem.api-v1+json. 4 tags maximum; "
            "canonical_url prevents duplicate-content penalties when the same "
            "article also runs on your own site."
        ),
    ),
    "hashnode": PlatformSpec(
        id="hashnode",
        label="Hashnode",
        kind="api",
        publisher_module="publishers.hashnode",
        content_format="markdown",
        auth="bearer",
        token_env="HASHNODE_PAT",
        docs_url="https://apidocs.hashnode.com/",
        token_help="https://hashnode.com/settings/developer",
        tag_limit=5,
        max_title_words=12,
        max_title_chars=128,
        max_body_bytes=100_000,
        min_body_words=120,
        supports_canonical=True,
        supports_draft=True,
        default_daily_limit=1,
        cooldown_hours=24,
        notes=(
            "GraphQL at gql.hashnode.com. Requires a publication id, and the "
            "write mutations are gated on a Pro plan - a free publication "
            "returns FORBIDDEN. originalArticleURL is the canonical field."
        ),
    ),
    "medium": PlatformSpec(
        id="medium",
        label="Medium",
        kind="api",
        publisher_module="publishers.medium",
        content_format="html",
        auth="bearer",
        token_env="MEDIUM_TOKEN",
        docs_url="https://github.com/Medium/medium-api-docs",
        token_help="https://medium.com/me/settings (Integration tokens)",
        tag_limit=5,
        max_title_words=12,
        max_title_chars=100,
        max_body_bytes=50_000,
        supports_canonical=False,
        supports_tags=True,
        supports_draft=True,
        default_daily_limit=1,
        cooldown_hours=48,
        legacy=True,
        notes=(
            "LEGACY. Medium stopped issuing integration tokens on 2025-01-01 "
            "and allows no new integrations; existing tokens still work. "
            "Disabled by default - enable it only if you already hold a token, "
            "otherwise use manual mode."
        ),
    ),
    "wordpress": PlatformSpec(
        id="wordpress",
        label="WordPress (WordPress.com or self-hosted)",
        kind="api",
        publisher_module="publishers.wordpress",
        content_format="html",
        auth="basic",
        token_env="WP_APP_PASSWORD",
        docs_url="https://developer.wordpress.com/docs/api/",
        token_help=(
            "WordPress.com: Settings -> Applications -> Application password; "
            "self-hosted: Users -> Profile -> Application Passwords"
        ),
        submission_url="https://wordpress.com/posts",
        tag_limit=8,
        max_title_words=14,
        max_title_chars=200,
        max_body_bytes=200_000,
        supports_canonical=True,
        supports_tags=True,
        supports_draft=True,
        supports_categories=True,
        default_daily_limit=3,
        cooldown_hours=6,
        notes=(
            "Two flavours: wordpress_com (public-api.wordpress.com, OAuth "
            "bearer token or application password) and self_hosted "
            "(/wp-json/wp/v2/posts). Your own site is the natural canonical "
            "home, so publish here first and point the other platforms at it."
        ),
    ),
    "coderlegion": PlatformSpec(
        id="coderlegion",
        label="CoderLegion",
        kind="manual",
        publisher_module="publishers.coderlegion",
        content_format="markdown",
        auth="none",
        submission_url="https://coderlegion.com/publish-with-us",
        docs_url="https://coderlegion.com/publish-with-us",
        tag_limit=5,
        max_title_words=14,
        max_title_chars=140,
        supports_tags=True,
        supports_categories=True,
        default_daily_limit=1,
        cooldown_hours=24,
        notes=(
            "No publishing API. Articles are submitted through the web app and "
            "reviewed by their editorial team, so this target is hand-ed out "
            "as a ready-to-paste markdown file and only marked live once you "
            "confirm the published URL."
        ),
    ),
    "devdojo": PlatformSpec(
        id="devdojo",
        label="DevDojo",
        kind="manual",
        publisher_module="publishers.devdojo",
        content_format="markdown",
        auth="none",
        submission_url="https://devdojo.com/community/posts/write",
        docs_url="https://devdojo.com/community/posts",
        tag_limit=6,
        max_title_words=14,
        max_title_chars=140,
        supports_tags=True,
        default_daily_limit=1,
        cooldown_hours=24,
        notes=(
            "No public publishing API. Their 'Write a Post' form is the "
            "supported path, so the article is hand-ed out as markdown for you "
            "to paste and submit."
        ),
    ),
    "webhook": PlatformSpec(
        id="webhook",
        label="Generic webhook",
        kind="webhook",
        publisher_module="publishers.webhook",
        content_format="markdown",
        auth="url_env",
        token_env="WEBHOOK_URL",
        docs_url="",
        token_help="Any https endpoint that accepts the JSON payload described in the config comments",
        tag_limit=0,
        max_title_words=0,
        max_title_chars=0,
        max_body_bytes=500_000,
        supports_tags=True,
        supports_canonical=True,
        default_daily_limit=5,
        cooldown_hours=1,
        notes=(
            "Escape hatch for Discourse, Ghost, Zuplo or an internal CMS: one "
            "POST with {item, markdown, platforms}. Off by default."
        ),
    ),
}


# --------------------------------------------------------------------------- #
# Lookup helpers
# --------------------------------------------------------------------------- #


def get_platform(platform_id: str) -> Optional[PlatformSpec]:
    return PLATFORMS.get((platform_id or "").strip().lower())


def require_platform(platform_id: str) -> PlatformSpec:
    spec = get_platform(platform_id)
    if spec is None:
        raise ValueError(
            f"unknown platform {platform_id!r}; expected one of {', '.join(PLATFORM_IDS)}"
        )
    return spec


def known_ids() -> Tuple[str, ...]:
    return tuple(PLATFORMS)


def api_ids() -> List[str]:
    return [pid for pid in PLATFORM_IDS if PLATFORMS[pid].kind == "api"]


def manual_ids() -> List[str]:
    return [pid for pid in PLATFORM_IDS if PLATFORMS[pid].kind == "manual"]


def legacy_ids() -> List[str]:
    return [pid for pid in PLATFORM_IDS if PLATFORMS[pid].legacy]


def catalog() -> List[Dict[str, object]]:
    """Serialisable registry, for `platforms` and the UI's bootstrap payload."""
    return [PLATFORMS[pid].to_dict() for pid in PLATFORM_IDS]
