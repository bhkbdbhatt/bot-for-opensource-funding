"""Forum channel sender.

Two modes:

* **API mode** (`forum.api_url` + `forum.api_key_env` set) - POSTs JSON to a
  board endpoint. The expected contract is
  ``{"topic_url": ..., "title": ..., "body": ...}``.
* **Manual mode** (no api_url, the default) - prints the fully formatted post
  plus the target thread URL and drops a copy into `forum_outbox/` so a human
  can paste it. Nothing is transmitted by the bot.

Manual mode still counts against the daily forum quota and marks the sponsor
`contacted`, because the post has been *prepared and queued*. A loud warning is
printed for every manual post.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

try:  # pragma: no cover - import guard
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "The 'requests' package is required. Run: pip install -r requirements.txt"
    ) from exc

from config_loader import Config, resolve_secret
from logging_setup import get_logger
from models import Sponsor, is_valid_url

LOG = get_logger("forum-sender")

_TITLE_RE = re.compile(r"^\s*title\s*:\s*(?P<title>.+?)\s*$", re.IGNORECASE)
DEFAULT_OUTBOX = "forum_outbox"


class ForumError(Exception):
    """Raised for unrecoverable forum posting problems."""


class ForumSender:
    """Publish outreach to the Genesys community forum."""

    channel = "forum"

    def __init__(self, config: Config, *, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = bool(dry_run)
        self.name = config.str("forum.name", "Genesys Cloud Community")
        self.topic_url = config.str("forum.topic_url")
        self.api_url = config.str("forum.api_url")
        self.api_key_env = config.str("forum.api_key_env")
        self.auth_header = config.str("forum.auth_header", "Authorization")
        self.auth_scheme = config.str("forum.auth_scheme", "Bearer")
        self.timeout = config.int("forum.timeout_seconds", 30, minimum=5, maximum=300)
        self.title_template = config.str("forum.title_template", "Open-source plugin: {feature}")
        self.outbox_dir = config.path("forum.outbox_dir", DEFAULT_OUTBOX)

    # -- configuration ----------------------------------------------------- #

    @property
    def api_key(self) -> str:
        return resolve_secret(self.api_key_env)

    @property
    def mode(self) -> str:
        if self.dry_run:
            return "dry-run"
        return "api" if self.api_url else "manual"

    def describe(self) -> Dict[str, Any]:
        return {
            "forum": self.name,
            "topic_url": self.topic_url,
            "mode": self.mode,
            "api_url": self.api_url or None,
            "api_key_env": self.api_key_env,
            "api_key_set": bool(self.api_key),
        }

    def check_ready(self) -> Tuple[bool, str]:
        if not self.topic_url or not is_valid_url(self.topic_url):
            return False, "forum.topic_url must be a valid http(s) URL"
        if self.mode == "api" and not self.api_key and self.api_key_env:
            return False, f"environment variable {self.api_key_env} is not set"
        return True, "ok"

    # -- gate -------------------------------------------------------------- #

    def can_send(self, sponsor: Sponsor) -> Tuple[bool, str]:
        target = sponsor.target()
        if not target:
            return False, f"no forum target on file for {sponsor.name!r}"
        if self.dry_run:
            return True, "dry-run"
        ready, reason = self.check_ready()
        return (False, reason) if not ready else (True, "ok")

    # -- composition ------------------------------------------------------- #

    def split_title(self, message: str) -> Tuple[str, str]:
        text = (message or "").strip()
        if "\n" in text:
            first, _, rest = text.partition("\n")
            match = _TITLE_RE.match(first)
            if match:
                return match.group("title").strip(), rest.strip()
        return self._default_title(), text

    def _default_title(self) -> str:
        """Title used when the generated message carries no `Title:` line."""
        features = self.config.list("plugin.features")
        headline = ""
        if features:
            headline = re.split(r" - | with ", features[0])[0].strip()[:60]
        try:
            return self.title_template.format(
                plugin_name=self.config.str("plugin.name"),
                feature=headline or self.config.str("plugin.name"),
            )[:120]
        except (KeyError, IndexError):
            return f"Open-source plugin from {self.name}"

    # -- delivery ---------------------------------------------------------- #

    def send(self, sponsor: Sponsor, message: str) -> bool:
        allowed, reason = self.can_send(sponsor)
        if not allowed:
            LOG.error("Cannot post for %s: %s", sponsor.name, reason)
            return False

        title, body = self.split_title(message)
        if not body:
            LOG.error("Refusing to publish an empty body for %s", sponsor.name)
            return False

        if self.mode == "dry-run":
            LOG.info("[DRY RUN] forum post -> %s | %s", sponsor.name, title)
            LOG.debug("Dry-run body:\n%s", body)
            return True

        if self.mode == "manual":
            return self._manual(sponsor, title, body)

        return self._api(sponsor, title, body)

    def _api(self, sponsor: Sponsor, title: str, body: str) -> bool:
        payload = {
            "topic_url": self.topic_url,
            "forum": self.name,
            "title": title,
            "body": body,
            "tags": ["appfoundry", "open-source"],
            "author": self.config.str("plugin.maintainer_name") or "plugin maintainer",
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            value = f"{self.auth_scheme} {self.api_key}".strip() if self.auth_scheme else self.api_key
            headers[self.auth_header] = value
        try:
            response = requests.post(
                self.api_url, json=payload, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as exc:
            LOG.error("Forum API request failed for %s: %s", sponsor.name, exc)
            return False

        if response.status_code >= 400:
            detail = (response.text or "").strip()[:300]
            LOG.error(
                "Forum API rejected the post for %s: HTTP %s %s",
                sponsor.name,
                response.status_code,
                detail,
            )
            return False

        location = ""
        try:
            body_json = response.json()
            if isinstance(body_json, dict):
                location = str(body_json.get("url") or body_json.get("location") or "")
        except ValueError:
            location = response.headers.get("Location", "")
        LOG.info("Posted to forum for %s | %s | %s", sponsor.name, title, location or "(no url returned)")
        return True

    def _manual(self, sponsor: Sponsor, title: str, body: str) -> bool:
        LOG.warning(
            "MANUAL POST REQUIRED for %s - no forum API configured. "
            "Copy the text below into %s",
            sponsor.name,
            self.topic_url,
        )
        saved = self._save_outbox(sponsor, title, body)
        banner = "=" * 72
        print(banner)
        print(f"FORUM POST (manual) -> {sponsor.name}  [{self.name}]")
        print(f"Thread: {self.topic_url}")
        print(f"Title : {title}")
        if saved:
            print(f"Saved : {saved}")
        print("-" * 72)
        print(body)
        print(banner)
        return True

    def _save_outbox(self, sponsor: Sponsor, title: str, body: str) -> Optional[str]:
        try:
            self.outbox_dir.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^A-Za-z0-9._-]+", "-", sponsor.name).strip("-") or "sponsor"
            path = self.outbox_dir / f"{slug}.md"
            path.write_text(
                f"<!-- {self.topic_url} -->\n"
                f"<!-- forum: {self.name} | sponsor: {sponsor.name} -->\n\n"
                f"# {title}\n\n{body}\n",
                encoding="utf-8",
            )
            return str(path)
        except OSError as exc:
            LOG.warning("Could not write forum outbox %s: %s", self.outbox_dir, exc)
            return None

    def preview(self, sponsor: Sponsor, message: str) -> Dict[str, Any]:
        """Non-destructive rendering, used by `generate`."""
        title, body = self.split_title(message)
        return {
            "channel": self.channel,
            "mode": self.mode,
            "forum": self.name,
            "topic_url": self.topic_url,
            "title": title,
            "body": body,
        }