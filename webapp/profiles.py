"""Profile store: one self-contained campaign per directory.

A *profile* is a directory ``profiles/<id>/`` holding a complete, standard
``config.yaml`` plus that campaign's runtime state (tracker, discovery cache,
log, forum outbox). Because :class:`~config_loader.Config` resolves relative
paths against the config file's own directory, storing the config *inside* the
profile directory gives every campaign isolated state for free.

Profiles are ordinary config files, so the UI is generic: point it at a Genesys
campaign, a different open-source project, or anything else, and the same wizard
applies.
"""

from __future__ import annotations

import copy
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - import guard
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required. Run: pip install -r requirements.txt"
    ) from exc

from config_loader import Config, ConfigError
from models import CHANNELS

PROFILES_DIRNAME = "profiles"
CONFIG_FILENAME = "config.yaml"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_ALLOWED_TOPIC = re.compile(r"[^a-z0-9._-]+")


def slugify(value: str) -> str:
    """Lowercase, hyphenated, filesystem-safe identifier."""
    slug = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return slug[:48] or "profile"


# --------------------------------------------------------------------------- #
# Templates (must satisfy config_loader.Config._validate)
# --------------------------------------------------------------------------- #


def _paths_block() -> Dict[str, Any]:
    return {
        "tracker_file": "sponsors.json",
        "discovery_cache": "discovered.json",
        "content_file": "content.json",
        "log_file": "bot.log",
        "log_level": "INFO",
        "log_max_bytes": 2 * 1024 * 1024,
        "log_backup_count": 3,
    }


def _rate_limits_block() -> Dict[str, Any]:
    return {
        "max_emails_per_day": 15,
        "max_forum_posts_per_day": 3,
        "min_hours_between_attempts": 72,
        "max_consecutive_failures": 5,
        # Content syndication has its own budget, deliberately separate from
        # outreach: publishing publicly is a different risk from emailing a
        # named contact, and the two must never compete for the same allowance.
        "max_publishes_per_day": 3,
        "min_hours_between_platform_posts": 24,
        "max_content_failures": 3,
        "platform_daily_limits": {
            "devto": 1,
            "hashnode": 1,
            "medium": 1,
            "wordpress": 2,
            "coderlegion": 1,
            "devdojo": 1,
            "webhook": 3,
        },
    }


def _scheduler_block() -> Dict[str, Any]:
    return {
        "interval_seconds": 7200,
        "batch_size": 5,
        "dry_run": False,
        "llm_command": [],
        "llm_timeout_seconds": 120,
    }


def _publishing_block() -> Dict[str, Any]:
    return {
        "dry_run": False,
        "batch_size": 3,
        "interval_seconds": 10800,
        "require_approval": True,
        "enforce_quality_gate": True,
        "request_delay_seconds": 2.0,
        "timeout_seconds": 30,
        "outbox_dir": "content_outbox",
    }


def _content_block() -> Dict[str, Any]:
    return {
        "enabled": False,
        "audience": "",
        "angle": "",
        "tone": "practitioner writing for peers, no marketing voice",
        "persona": "an engineer who maintains the project",
        "disclosure": "I maintain this project.",
        "disclosure_required": True,
        "disclosure_note": (
            "Full disclosure: I build and maintain this project. Every claim below "
            "is something I have run in production, and I have linked the code so "
            "you can check it rather than take my word for it."
        ),
        "license": "",
        "sections": [
            "The problem",
            "What the project does",
            "How it is built",
            "Try it",
            "Where it stops being useful",
            "Feedback",
        ],
        "tags": ["opensource", "devops"],
        "default_platforms": [],
        "canonical_base_url": "",
        "call_to_action": "",
        "closing": "",
        "min_words": 350,
        "max_words": 1800,
        "max_title_words": 12,
        "summary_max_chars": 300,
        "forbid_words": [],
        "topics": [],
    }


def _platforms_block(enabled: Optional[List[str]] = None) -> Dict[str, Any]:
    """Every target, off by default. ``enabled`` opts a subset in.

    Written out in full rather than left empty so the wizard has real fields to
    bind and an operator can see every knob without opening config.example.yaml.
    """
    on = {pid for pid in (enabled or [])}
    return {
        "devto": {
            "enabled": "devto" in on,
            "api_base": "https://dev.to/api",
            "api_key_env": "DEVTO_API_KEY",
            "organization_username": "",
            "state_published": False,
            "series": "",
        },
        "hashnode": {
            "enabled": "hashnode" in on,
            "api_base": "https://gql.hashnode.com",
            "api_key_env": "HASHNODE_PAT",
            "publication_id": "",
            "publish_immediately": True,
            "enable_toc": True,
            "subtitle": "",
        },
        "medium": {
            # Legacy: Medium issues no new integration tokens (since 2025-01-01).
            # Left off; see the note rendered by `platforms`.
            "enabled": False,
            "api_base": "https://api.medium.com/v1",
            "api_key_env": "MEDIUM_TOKEN",
            "author_id": "",
            "publication_id": "",
            "content_format": "html",
            "publish_status": "public",
            "license": "all-rights-reserved",
        },
        "wordpress": {
            "enabled": "wordpress" in on,
            "flavor": "wordpress_com",
            "site": "",
            "site_url": "",
            "auth_mode": "application_password",
            "username_env": "WP_USERNAME",
            "password_env": "WP_APP_PASSWORD",
            "oauth_env": "WPCOM_OAUTH_TOKEN",
            "status": "publish",
            "categories": [],
        },
        "coderlegion": {
            "enabled": "coderlegion" in on,
            "submit_url": "https://coderlegion.com/publish-with-us",
            "guidelines_url": "https://coderlegion.com/publish-with-us",
            "categories": ["Articles"],
            "tags": [],
        },
        "devdojo": {
            "enabled": "devdojo" in on,
            "submit_url": "https://devdojo.com/community/posts/write",
            "guidelines_url": "https://devdojo.com/community/posts",
            "tags": [],
        },
        "webhook": {
            "enabled": False,
            "url_env": "WEBHOOK_URL",
            "method": "POST",
            "auth_header": "Authorization",
            "auth_scheme": "Bearer",
            "include_markdown": True,
        },
    }


def _email_block(sender: str, username: str) -> Dict[str, Any]:
    return {
        "enabled": True,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "use_tls": True,
        "use_ssl": False,
        "username": username,
        "sender": sender,
        "password_env": "SMTP_PASSWORD",
        "timeout_seconds": 30,
        "subject_template": "{feature} for your team",
        "html_template": "",
    }


def _forum_block(name: str, topic_url: str) -> Dict[str, Any]:
    return {
        "enabled": False,
        "name": name,
        "topic_url": topic_url,
        "api_url": "",
        "api_key_env": "FORUM_API_KEY",
        "auth_header": "Authorization",
        "auth_scheme": "Bearer",
        "timeout_seconds": 30,
        "title_template": "Open-source project: {feature}",
        "outbox_dir": "forum_outbox",
    }


def _github_block(topics: List[str]) -> Dict[str, Any]:
    return {
        "api_base": "https://api.github.com",
        "token_env": "GITHUB_TOKEN",
        "user_agent": "OutreachWizard/1.0 (+https://github.com/)",
        "topics": topics,
        "per_page": 100,
        "max_pages": 3,
        "request_delay_seconds": 0.4,
        "timeout_seconds": 20,
        "filters": {
            "min_org_repos": 5,
            "min_individual_genesys_repos": 3,
            "min_stars": 0,
            "exclude_forks": True,
            "max_owner_lookups": 40,
        },
        "scrape_public_emails": True,
        "email_scrape_max_sites": 3,
    }


def generic_template() -> Dict[str, Any]:
    """A valid, neutral campaign the wizard can edit."""
    return {
        "plugin": {
            "name": "Your Open-Source Project",
            "description": (
                "Open-source project that removes recurring integration work. "
                "Actively maintained by its contributors."
            ),
            "repo_url": "https://github.com/your-org/your-project",
            "appfoundry_url": "",
            "demo_url": "",
            "maintainer_name": "Your Name",
            "maintainer_email": "you@example.com",
            "value_prop": (
                "Stop rebuilding the same integration in every project - this "
                "project ships the pattern as a tested, reusable building block."
            ),
            "features": [
                "Drop-in API wrapper with retry and token handling",
                "Event-driven architecture with no server to operate",
                "Example flows and reference implementations",
                "Permissive licence, no vendor lock-in",
            ],
            "cta": "Would a 15-minute walkthrough be useful?",
            "ask": (
                "We are looking for organisations willing to sponsor ongoing "
                "maintenance: CI, docs and the contributor rotation."
            ),
            "unsubscribe_note": (
                'If this is not relevant to you, reply "unsubscribe" and you '
                "will not hear from this bot again."
            ),
        },
        "email": _email_block(
            "Project Bot <bot@example.com>", "bot@example.com"
        ),
        "forum": _forum_block("Community Forum", "https://community.example.com/"),
        "github": _github_block(["your-topic"]),
        "content": _content_block(),
        "platforms": _platforms_block(),
        "publishing": _publishing_block(),
        "sponsors": [],
        "rate_limits": _rate_limits_block(),
        "scheduler": _scheduler_block(),
        "paths": _paths_block(),
    }


def genesys_template() -> Dict[str, Any]:
    """A ready-to-go campaign for the Genesys Cloud ecosystem."""
    data = generic_template()
    data["plugin"].update(
        {
            "name": "Genesys Cloud Community Plugin",
            "description": (
                "Open-source plugin for Genesys Cloud that reduces the "
                "custom-development cost of common integration patterns. MIT "
                "licensed, actively maintained by contributors who build on "
                "Genesys Cloud every day."
            ),
            "repo_url": "https://github.com/purecloudlabs/community-plugin-example",
            "appfoundry_url": "https://appfoundry.genesys.cloud/",
            "value_prop": (
                "Stop rebuilding the same Genesys Cloud integration in every "
                "project - this plugin ships the pattern as a tested, reusable "
                "building block."
            ),
            "features": [
                "Drop-in Genesys Cloud API wrapper with retry and token handling",
                "Event-driven architecture (Pub/Sub) with no server to operate",
                "Example flows for messaging, notifications and call control",
                "MIT licensed, no vendor lock-in, community supported",
            ],
            "cta": (
                "Would a 15-minute walkthrough be useful? Happy to share the "
                "repo, the architecture notes and the roadmap."
            ),
            "ask": (
                "We are looking for organisations willing to sponsor ongoing "
                "maintenance of this plugin. Sponsorship covers CI, docs and "
                "the contributor rotation."
            ),
        }
    )
    data["email"] = _email_block(
        "Genesys Plugin Bot <bot@example.com>", "bot@example.com"
    )
    data["forum"] = _forum_block(
        "Genesys Cloud Community - AppFoundry",
        "https://community.genesys.cloud/discussion/appfoundry",
    )
    data["github"] = _github_block(["genesys", "genesys-cloud"])
    data["content"].update(
        {
            "audience": (
                "Genesys Cloud developers and contact-centre architects who keep "
                "rebuilding the same integrations in each new project."
            ),
            "angle": (
                "The hard part of any Genesys Cloud integration is not the API "
                "calls, it is the token lifecycle, the retry policy and the "
                "idempotency around them - and that part should be written once, "
                "tested once, and reused."
            ),
            "tags": ["genesys", "genesys-cloud", "cloud", "opensource"],
            "license": "MIT",
            "call_to_action": (
                "If you have hit the same token-refresh or duplicate-write "
                "problems, I would like to hear how you solved them."
            ),
        }
    )
    return data


PRESETS: Dict[str, Dict[str, Any]] = {
    "genesys": {
        "label": "Genesys Cloud plugin",
        "description": "Topics genesys / genesys-cloud, AppFoundry links.",
        "factory": genesys_template,
    },
    "generic": {
        "label": "Generic project",
        "description": "Neutral placeholders - edit everything.",
        "factory": generic_template,
    },
}


def preset_catalog() -> List[Dict[str, str]]:
    return [
        {"id": key, "label": value["label"], "description": value["description"]}
        for key, value in PRESETS.items()
    ]


def template_for(preset: str) -> Dict[str, Any]:
    chosen = PRESETS.get((preset or "").strip().lower()) or PRESETS["generic"]
    return chosen["factory"]()


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #


@dataclass
class Profile:
    id: str
    path: Path
    data: Dict[str, Any]

    def config_path(self) -> Path:
        return self.path / CONFIG_FILENAME

    def describe(self) -> Dict[str, Any]:
        plugin = self.data.get("plugin") if isinstance(self.data.get("plugin"), dict) else {}
        github = self.data.get("github") if isinstance(self.data.get("github"), dict) else {}
        topics = github.get("topics") or []
        if isinstance(topics, str):
            topics = [part.strip() for part in topics.split(",") if part.strip()]
        try:
            mtime = self.config_path().stat().st_mtime
        except OSError:
            mtime = 0.0
        return {
            "id": self.id,
            "name": str(plugin.get("name") or self.id),
            "repo_url": str(plugin.get("repo_url") or ""),
            "topics": [str(topic) for topic in topics],
            "config_file": str(self.config_path()),
            "updated_at": mtime,
        }


class ProfileStore:
    """Manages ``profiles/<id>/config.yaml`` directories."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.root = self.base_dir / PROFILES_DIRNAME
        self.root.mkdir(parents=True, exist_ok=True)

    # -- discovery --------------------------------------------------------- #

    def _profile_dirs(self) -> List[Path]:
        return sorted(
            (
                entry
                for entry in self.root.iterdir()
                if entry.is_dir() and (entry / CONFIG_FILENAME).is_file()
            ),
            key=lambda item: item.name.lower(),
        )

    def list(self) -> List[Profile]:
        profiles: List[Profile] = []
        for directory in self._profile_dirs():
            try:
                data = self._read_raw(directory / CONFIG_FILENAME)
            except (OSError, yaml.YAMLError):
                data = {}
            profiles.append(Profile(id=directory.name, path=directory, data=data))
        return profiles

    def ids(self) -> List[str]:
        return [profile.id for profile in self.list()]

    def exists(self, pid: str) -> bool:
        return self.config_path(pid).is_file()

    def get(self, pid: str) -> Optional[Profile]:
        if not self.exists(pid):
            return None
        return Profile(id=pid, path=self.root / pid, data=self.load_raw(pid))

    def path_for(self, pid: str) -> Path:
        return self.root / pid

    def config_path(self, pid: str) -> Path:
        return self.path_for(pid) / CONFIG_FILENAME

    # -- default bootstrap ------------------------------------------------- #

    def ensure_default(self) -> Optional[str]:
        """Create a first profile by importing the project config, if needed."""
        if self.ids():
            return None
        for candidate in ("config.yaml", "config.example.yaml"):
            source = self.base_dir / candidate
            if source.is_file():
                try:
                    data = self._read_raw(source)
                except (OSError, yaml.YAMLError):
                    continue
                return self.write("default", data, create_only=True)
        return self.write("default", generic_template(), create_only=True)

    # -- read / write ------------------------------------------------------ #

    @staticmethod
    def _read_raw(path: Path) -> Dict[str, Any]:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return raw if isinstance(raw, dict) else {}

    def load_raw(self, pid: str) -> Dict[str, Any]:
        return self._read_raw(self.config_path(pid))

    def load_config(self, pid: str) -> Config:
        return Config.load(self.config_path(pid), create_from_example=False)

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, path)
        except OSError:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

    def write(
        self, pid: str, data: Dict[str, Any], *, create_only: bool = False
    ) -> str:
        """Persist a profile's config and return the profile id."""
        if create_only and self.exists(pid):
            raise FileExistsError(pid)
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        self._atomic_write(self.config_path(pid), text)
        return pid

    def create(self, name: str, preset: str = "generic") -> Profile:
        pid = slugify(name)
        if self.exists(pid):
            raise FileExistsError(f"a profile named {pid!r} already exists")
        data = copy.deepcopy(template_for(preset))
        if preset != "genesys" and name.strip():
            data.setdefault("plugin", {})["name"] = name.strip()
        self.write(pid, data, create_only=True)
        profile = self.get(pid)
        assert profile is not None  # just written
        return profile

    def delete(self, pid: str) -> None:
        if not self.exists(pid):
            raise FileNotFoundError(pid)
        directory = self.path_for(pid)
        # Only ever remove a directory that looks like a profile.
        if directory.parent.resolve() != self.root.resolve():
            raise ValueError(f"refusing to delete {directory}")
        for child in sorted(directory.rglob("*"), reverse=True):
            try:
                child.unlink() if child.is_file() else child.rmdir()
            except OSError:
                pass
        try:
            directory.rmdir()
        except OSError as exc:
            raise OSError(f"could not remove {directory}: {exc}") from exc

    # -- validation -------------------------------------------------------- #

    @staticmethod
    def validate_data(pid: str, data: Dict[str, Any], path: Path) -> List[str]:
        """Return a list of validation errors ([] == valid)."""
        try:
            Config(copy.deepcopy(data), path)
        except ConfigError as exc:
            lines = str(exc).splitlines()
            messages = [line.strip().lstrip("-").strip() for line in lines[1:]]
            return [message for message in messages if message] or [str(exc)]
        return []

    def validate(self, pid: str, data: Dict[str, Any]) -> List[str]:
        return self.validate_data(pid, data, self.config_path(pid))

    # -- convenience ------------------------------------------------------- #

    def runtime_paths(self, pid: str, config: Config) -> Dict[str, Path]:
        return {
            "tracker": config.path("paths.tracker_file", "sponsors.json"),
            "discovery_cache": config.path("paths.discovery_cache", "discovered.json"),
            "log_file": config.path("paths.log_file", "bot.log"),
        }


def normalize_topic(value: str) -> str:
    """Normalize a GitHub topic token the way discovery.py expects."""
    return _ALLOWED_TOPIC.sub("-", (value or "").strip().lstrip("#").lower()).strip("-")


CHANNEL_CHOICES = list(CHANNELS)

#: Flattened `content.*` bindings the wizard renders, so the Content step stays
#: declarative instead of hard-coding ~20 field() calls.
CONTENT_BINDINGS = (
    ("enabled", "Content syndication enabled", "check"),
    ("audience", "Audience", "area"),
    ("angle", "Editorial angle (the thesis)", "area"),
    ("tone", "Tone", "text"),
    ("persona", "Who is speaking", "text"),
    ("disclosure", "Disclosure line (top of every article)", "text"),
    ("disclosure_required", "Refuse articles without a disclosure", "check"),
    ("disclosure_note", "Disclosure note (footer)", "area"),
    ("license", "Licence", "text"),
    ("tags", "Default tags (one per line)", "list"),
    ("canonical_base_url", "Canonical URL (where the article really lives)", "text"),
    ("call_to_action", "Call to action", "area"),
    ("closing", "Closing paragraph", "area"),
    ("sections", "Sections (one per line)", "list"),
    ("min_words", "Minimum words", "number"),
    ("max_words", "Maximum words", "number"),
    ("max_title_words", "Maximum title words", "number"),
    ("forbid_words", "Extra banned words (one per line)", "list"),
)


def platform_bindings() -> List[Dict[str, Any]]:
    """Per-platform rows for the wizard's platform list.

    `field` is the *meaningful* knob for that platform, chosen from its
    `PlatformSpec` rather than hard-coded here, so a new registry entry shows up
    in the UI without touching this module.
    """
    from platforms import PLATFORMS, PLATFORM_IDS

    rows: List[Dict[str, Any]] = []
    for platform_id in PLATFORM_IDS:
        spec = PLATFORMS[platform_id]
        rows.append(
            {
                "id": spec.id,
                "label": spec.label,
                "kind": spec.kind,
                "legacy": spec.legacy,
                "token_env": spec.token_env,
                "token_help": spec.token_help,
                "docs_url": spec.docs_url,
                "submission_url": spec.submission_url,
                "tag_limit": spec.tag_limit,
                "notes": spec.notes,
            }
        )
    return rows
