"""Typed access to config.yaml with validation and helpful error messages."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:  # pragma: no cover - import guard
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required. Run: pip install -r requirements.txt"
    ) from exc

from models import (
    CHANNELS,
    CHANNEL_EMAIL,
    CHANNEL_FORUM,
    CONTENT_STATUSES,
    PLATFORM_IDS,
    STATUSES,
    is_valid_url,
)
from platforms import PLATFORMS

CONFIG_FILENAME = "config.yaml"
EXAMPLE_FILENAME = "config.example.yaml"


class ConfigError(Exception):
    """Raised when config.yaml is missing, malformed or invalid."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "on", "1"}:
        return True
    if text in {"false", "no", "n", "off", "0", ""}:
        return False
    return default


def _as_int(value: Any, default: int, *, minimum: int = 0, maximum: Optional[int] = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < minimum:
        return minimum
    if maximum is not None and number > maximum:
        return maximum
    return number


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def resolve_secret(env_name: str) -> str:
    """Read a secret from the environment (never stored in config)."""
    if not env_name:
        return ""
    return os.environ.get(env_name, "").strip()


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


class Config:
    """Dotted-path reader over the parsed YAML document."""

    def __init__(self, data: Dict[str, Any], path: Path) -> None:
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: top level of the config must be a mapping")
        self._data = data
        self.file_path = path
        self.base_dir = path.parent.resolve()
        self.warnings: List[str] = []
        self._validate()

    # -- construction ------------------------------------------------------ #

    @classmethod
    def load(cls, path: Optional[str | Path] = None, *, create_from_example: bool = True) -> "Config":
        if path:
            return cls._read(Path(path).expanduser(), create_from_example=create_from_example)

        for directory in cls._search_dirs():
            candidate = directory / CONFIG_FILENAME
            if candidate.is_file():
                return cls._read(candidate, create_from_example=create_from_example)

        # Nothing on disk yet: bootstrap from the example shipped beside this
        # module, not from whatever directory the user happened to be in.
        package_dir = Path(__file__).resolve().parent
        return cls._read(package_dir / CONFIG_FILENAME, create_from_example=create_from_example)

    @staticmethod
    def _search_dirs() -> List[Path]:
        """cwd first, then the directory holding this module.

        The second entry means `python /elsewhere/main.py list` still finds the
        config that lives next to the code.
        """
        directories = [Path.cwd()]
        package_dir = Path(__file__).resolve().parent
        if package_dir != directories[0]:
            directories.append(package_dir)
        return directories

    @classmethod
    def _read(cls, target: Path, *, create_from_example: bool) -> "Config":
        if not target.is_file():
            example = target.parent / EXAMPLE_FILENAME
            if not example.is_file():
                raise ConfigError(
                    f"Config not found: {target}\n"
                    f"Expected a {CONFIG_FILENAME} beside {EXAMPLE_FILENAME}; "
                    f"the example documents every supported key."
                )
            if not create_from_example:
                raise ConfigError(f"Config not found: {target}")
            target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"{target}: invalid YAML - {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"{target}: cannot read file - {exc}") from exc
        return cls(raw or {}, target)

    # -- reading ----------------------------------------------------------- #

    @property
    def raw(self) -> Dict[str, Any]:
        return copy.deepcopy(self._data)

    def section(self, name: str) -> Dict[str, Any]:
        """A mapping section. Accepts a dotted path (``rate_limits.platform_daily_limits``)."""
        value = self.get(name)
        return value if isinstance(value, dict) else {}

    def records(self, name: str) -> List[Dict[str, Any]]:
        """A list-of-mappings section (e.g. `sponsors`), skipping junk entries.

        Accepts a dotted path for the same reason `section()` does.
        """
        value = self.get(name)
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def str(self, dotted: str, default: str = "") -> str:
        return _as_str(self.get(dotted), default)

    def int(self, dotted: str, default: int = 0, *, minimum: int = 0, maximum: Optional[int] = None) -> int:
        return _as_int(self.get(dotted), default, minimum=minimum, maximum=maximum)

    def float(self, dotted: str, default: float = 0.0) -> float:
        try:
            return float(self.get(dotted, default))
        except (TypeError, ValueError):
            return default

    def bool(self, dotted: str, default: bool = False) -> bool:
        return _as_bool(self.get(dotted), default)

    def list(self, dotted: str, default: Optional[Iterable[str]] = None) -> List[str]:
        value = self.get(dotted)
        if value is None:
            return list(default or [])
        return _as_list(value)

    def path(self, dotted: str, default: str) -> Path:
        value = Path(self.str(dotted, default) or default).expanduser()
        return value if value.is_absolute() else (self.base_dir / value)

    def secret(self, dotted: str) -> str:
        """Resolve ``<section>.<key>_env`` into the actual environment value."""
        return resolve_secret(self.str(dotted))

    # -- validation -------------------------------------------------------- #

    def _validate(self) -> None:
        errors: List[str] = []
        warnings: List[str] = []

        plugin = self.section("plugin")
        if not plugin:
            errors.append("missing required section: plugin")
        else:
            if not _as_str(plugin.get("name")):
                errors.append("plugin.name is required")
            for url_key in ("repo_url", "appfoundry_url"):
                value = _as_str(plugin.get(url_key))
                if value and not is_valid_url(value):
                    errors.append(f"plugin.{url_key} must be an http(s) URL (got {value!r})")
            if not _as_list(plugin.get("features")):
                errors.append("plugin.features must contain at least one entry")

        email = self.section("email")
        if email:
            if not _as_str(email.get("smtp_host")):
                errors.append("email.smtp_host is required")
            port = _as_int(email.get("smtp_port"), 0)
            if not 1 <= port <= 65535:
                errors.append(f"email.smtp_port must be 1-65535 (got {port})")
            if _as_bool(email.get("use_tls"), False) and _as_bool(email.get("use_ssl"), False):
                errors.append("email.use_tls and email.use_ssl cannot both be true")
            if not _as_str(email.get("sender")):
                errors.append("email.sender is required (RFC 5322 'Name <addr>')")
            pw_env = _as_str(email.get("password_env"))
            if pw_env and not resolve_secret(pw_env):
                # Not fatal - only matters when actually sending mail.
                warnings.append(
                    f"environment variable {pw_env!r} (email.password_env) is not set - "
                    "email delivery will be refused until it is"
                )

        forum = self.section("forum")
        if forum:
            api_url = _as_str(forum.get("api_url"))
            if api_url and not is_valid_url(api_url):
                errors.append("forum.api_url must be an http(s) URL")
            key_env = _as_str(forum.get("api_key_env"))
            if api_url and key_env and not resolve_secret(key_env):
                warnings.append(
                    f"environment variable {key_env!r} (forum.api_key_env) is not set - "
                    "the sender will refuse to post"
                )
            if not _as_str(forum.get("topic_url")):
                errors.append("forum.topic_url is required (used for manual posting)")

        github = self.section("github")
        if github:
            api_base = _as_str(github.get("api_base"), "https://api.github.com")
            if not is_valid_url(api_base):
                errors.append(f"github.api_base must be an http(s) URL (got {api_base!r})")
            topics = _as_list(github.get("topics"))
            if not topics:
                errors.append("github.topics must contain at least one topic")
            if self.int("github.per_page", 100, minimum=1, maximum=100) < 1:
                errors.append("github.per_page must be between 1 and 100")

        sponsors = self._data.get("sponsors") or []
        if not isinstance(sponsors, list):
            errors.append("sponsors must be a list of mappings")
        else:
            for index, entry in enumerate(sponsors):
                if not isinstance(entry, dict):
                    errors.append(f"sponsors[{index}] must be a mapping")
                    continue
                name = _as_str(entry.get("name"))
                if not name:
                    errors.append(f"sponsors[{index}].name is required")
                channel = _as_str(entry.get("channel"), CHANNEL_EMAIL).lower()
                if channel not in CHANNELS:
                    errors.append(
                        f"sponsors[{index}].channel must be one of {CHANNELS} (got {channel!r})"
                    )
                status = _as_str(entry.get("status"), "new").lower()
                if status not in STATUSES:
                    errors.append(
                        f"sponsors[{index}].status must be one of {STATUSES} (got {status!r})"
                    )

        limits = self.section("rate_limits")
        if limits:
            if self.int("rate_limits.max_emails_per_day", 15, maximum=100000) < 1:
                errors.append("rate_limits.max_emails_per_day must be >= 1")
            if self.int("rate_limits.max_forum_posts_per_day", 3, maximum=100000) < 1:
                errors.append("rate_limits.max_forum_posts_per_day must be >= 1")

        self._validate_content(errors, warnings)
        self._validate_platforms(errors, warnings)

        scheduler = self.section("scheduler")
        if scheduler:
            if self.int("scheduler.interval_seconds", 7200, minimum=1) < 1:
                errors.append("scheduler.interval_seconds must be >= 1")
            if self.int("scheduler.batch_size", 5, maximum=100) < 1:
                errors.append("scheduler.batch_size must be between 1 and 100")
            for index, token in enumerate(_as_list(scheduler.get("llm_command"))):
                if any(char in token for char in "\r\n"):
                    errors.append(f"scheduler.llm_command[{index}] must not contain newlines")

        publishing = self.section("publishing")
        if publishing:
            if self.int("publishing.interval_seconds", 10800, minimum=60) < 60:
                errors.append("publishing.interval_seconds must be >= 60")
            if self.int("publishing.batch_size", 3, minimum=1, maximum=50) < 1:
                errors.append("publishing.batch_size must be between 1 and 50")
            if self.float("publishing.request_delay_seconds", 2.0) < 0:
                errors.append("publishing.request_delay_seconds must be >= 0")

        if not (self.section("email") or self.section("forum")):
            errors.append("at least one channel (email or forum) must be configured")

        self.warnings = warnings
        if errors:
            joined = "\n".join(f"  - {message}" for message in errors)
            raise ConfigError(f"{self.file_path}: invalid configuration:\n{joined}")

    def _validate_content(self, errors: List[str], warnings: List[str]) -> None:
        """Validate the optional `content` section.

        Everything here is optional. A config with no `content` block still
        validates, because outreach is a complete use on its own and profiles
        written before this feature existed must keep working.
        """
        content = self.section("content")
        if not content:
            return
        if self.bool("content.enabled", False) and not self.platforms_enabled():
            warnings.append(
                "content.enabled is true but no platform is enabled - set "
                "platforms.<id>.enabled: true (see 'python main.py platforms')"
            )
        defaults = _as_list(content.get("default_platforms"))
        for index, platform in enumerate(defaults):
            if platform not in PLATFORM_IDS:
                errors.append(
                    f"content.default_platforms[{index}] must be one of "
                    f"{', '.join(PLATFORM_IDS)} (got {platform!r})"
                )
        if self.int("content.min_words", 350, maximum=50000) < 0:
            errors.append("content.min_words must be >= 0")
        max_words = self.int("content.max_words", 1800, minimum=50, maximum=100000)
        min_words = self.int("content.min_words", 350, maximum=50000)
        if min_words and max_words and min_words > max_words:
            errors.append("content.min_words must not exceed content.max_words")
        for index, topic in enumerate(self.records("content.topics")):
            if not _as_str(topic.get("title")):
                errors.append(f"content.topics[{index}].title is required")
            for jindex, platform in enumerate(_as_list(topic.get("platforms"))):
                if platform not in PLATFORM_IDS:
                    errors.append(
                        f"content.topics[{index}].platforms[{jindex}] must be one of "
                        f"{', '.join(PLATFORM_IDS)} (got {platform!r})"
                    )

    def _validate_platforms(self, errors: List[str], warnings: List[str]) -> None:
        """Validate the optional `platforms` block.

        Only keys that are actually present are checked, and unknown platform ids
        are an error rather than a silent no-op - a typo in a platform name would
        otherwise look like a successful configuration that never publishes.
        """
        section = self.section("platforms")
        if not section:
            return
        for platform_id, block in section.items():
            if platform_id not in PLATFORM_IDS:
                errors.append(
                    f"platforms.{platform_id} is not a known platform; "
                    f"expected one of {', '.join(PLATFORM_IDS)}"
                )
                continue
            if not isinstance(block, dict):
                errors.append(f"platforms.{platform_id} must be a mapping")
                continue
            spec = PLATFORMS[platform_id]
            if spec.legacy and _as_bool(block.get("enabled"), False):
                warnings.append(
                    f"platforms.{platform_id}.enabled is true, but this platform is legacy: "
                    f"{spec.notes}"
                )
            for url_key in ("api_base", "submit_url", "guidelines_url", "site_url", "url"):
                value = _as_str(block.get(url_key))
                if value and not is_valid_url(value):
                    errors.append(
                        f"platforms.{platform_id}.{url_key} must be an http(s) URL (got {value!r})"
                    )
            for key in ("api_key_env", "token_env", "oauth_env", "username", "password_env"):
                value = _as_str(block.get(key))
                if any(char in value for char in "\r\n"):
                    errors.append(f"platforms.{platform_id}.{key} must not contain newlines")
            if not _as_bool(block.get("enabled"), False):
                continue
            if spec.kind == "api" and not self._platform_credentials_present(platform_id, block):
                key_env = _as_str(block.get("api_key_env")) or _as_str(
                    block.get("token_env")
                ) or _as_str(block.get("oauth_env")) or spec.token_env
                warnings.append(
                    f"platforms.{platform_id} is enabled but environment variable "
                    f"{key_env or '(unnamed)'} is not set - publishing will be refused until it is"
                )

    def _platform_credentials_present(self, platform_id: str, block: Dict[str, Any]) -> bool:
        """Does any of this platform's credential indirections resolve to a value?"""
        candidates = (
            _as_str(block.get("api_key_env")),
            _as_str(block.get("token_env")),
            _as_str(block.get("oauth_env")),
            _as_str(block.get("password_env")),
            PLATFORMS[platform_id].token_env,
        )
        return any(resolve_secret(name) for name in candidates if name)

    def platforms_enabled(self) -> List[str]:
        """Platform ids with `enabled: true`, in registry order."""
        return [
            platform
            for platform in PLATFORM_IDS
            if self.bool(f"platforms.{platform}.enabled", False)
        ]

    # -- derived helpers --------------------------------------------------- #

    @property
    def github_token(self) -> str:
        return resolve_secret(self.str("github.token_env", "GITHUB_TOKEN"))

    @property
    def github_topics(self) -> List[str]:
        topics = [topic.lstrip("#") for topic in self.list("github.topics")]
        return topics or ["genesys"]

    @property
    def rate_limits(self) -> Dict[str, int]:
        return {
            CHANNEL_EMAIL: self.int("rate_limits.max_emails_per_day", 15, maximum=100000),
            CHANNEL_FORUM: self.int("rate_limits.max_forum_posts_per_day", 3, maximum=100000),
        }

    @property
    def content_statuses(self) -> List[str]:
        """Accepted `content` pipeline statuses, for the CLI and the UI."""
        return list(CONTENT_STATUSES)

    @property
    def publishing_enabled(self) -> bool:
        """Is content syndication switched on for this campaign?

        Requires both halves: the `content.enabled` switch and at least one
        enabled platform. Either alone is a no-op, so this is the single
        predicate the CLI, the UI and the promoter all ask.
        """
        return self.bool("content.enabled", False) and bool(self.platforms_enabled())

    @property
    def plugin(self) -> Dict[str, Any]:
        return self.section("plugin")

    def channel_enabled(self, channel: str) -> bool:
        return self.bool(f"{channel}.enabled", True)

    def describe(self) -> Dict[str, Any]:
        """Non-secret summary for `status`."""
        return {
            "config_file": str(self.file_path),
            "plugin": self.str("plugin.name"),
            "repo_url": self.str("plugin.repo_url"),
            "topics": self.github_topics,
            "github_token_set": bool(self.github_token),
            "email_enabled": self.channel_enabled(CHANNEL_EMAIL),
            "smtp": f"{self.str('email.smtp_host')}:{self.int('email.smtp_port', 0)}",
            "smtp_password_set": bool(resolve_secret(self.str("email.password_env"))),
            "forum_enabled": self.channel_enabled(CHANNEL_FORUM),
            "forum_mode": "api" if self.str("forum.api_url") else "manual",
            "rate_limits": self.rate_limits,
            "batch_size": self.int("scheduler.batch_size", 5, maximum=100),
            "interval_seconds": self.int("scheduler.interval_seconds", 7200, minimum=1),
            "dry_run": self.bool("scheduler.dry_run", False),
            "content_enabled": self.publishing_enabled,
            "content_dry_run": self.bool("publishing.dry_run", False),
            "content_platforms": self.platforms_enabled(),
            "content_batch_size": self.int("publishing.batch_size", 3, minimum=1, maximum=50),
            "content_interval_seconds": self.int(
                "publishing.interval_seconds", 10800, minimum=60
            ),
            "content_enforce_gate": self.bool("publishing.enforce_quality_gate", True),
        }