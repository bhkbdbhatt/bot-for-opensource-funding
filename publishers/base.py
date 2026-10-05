"""Publisher base: the contract, the outcome and the shared plumbing.

Where `senders/` answers *"did it send?"* with a bool, this package answers
*"what happened, where, and where can I see it?"* with a :class:`PublishOutcome`.
That difference is deliberate. The product of a syndication run is the ledger of
live URLs; a boolean would throw away the only thing worth keeping.

Three modes, mirroring how `ForumSender` degrades:

* **dry-run** - shape and validate the payload, transmit nothing.
* **api** / **webhook** - a documented public endpoint. `check_ready()` verifies
  the credential is present *before* the attempt, so a missing key is a skip
  rather than an HTTP 401 in the history.
* **manual** - the platform has no publishing API. Write the exact markdown to
  the outbox, print the submission URL and the instructions, and report
  ``queued``. Nothing is claimed as published.

Every HTTP call funnels through :meth:`BasePublisher._request`, which enforces
the timeout, maps transport errors and HTTP status onto one exception type, and
never lets a credential leak into a log line.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - import guard
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "The 'requests' package is required. Run: pip install -r requirements.txt"
    ) from exc

from logging_setup import get_logger
from models import MODE_API, MODE_DRY_RUN, MODE_MANUAL, MODE_WEBHOOK, ContentItem
from platforms import PlatformSpec, require_platform

LOG = get_logger("publishers")

DEFAULT_TIMEOUT = 30
DEFAULT_OUTBOX = "content_outbox"
_SLUG_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: HTTP status codes worth retrying once. Everything else is terminal: a 401 or
#: a 422 will fail identically on a second attempt.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class PublishError(Exception):
    """Raised for unrecoverable publisher problems."""


# --------------------------------------------------------------------------- #
# Outcome
# --------------------------------------------------------------------------- #


@dataclass
class PublishOutcome:
    """The result of one (article, platform) publish attempt.

    ``live`` distinguishes *publicly visible* from *accepted but not published*.
    Several targets can create a draft (DEV's ``published: false``, WordPress's
    ``status: draft``, Hashnode's ``createDraft``) and a draft's URL is not a
    live link - so the ledger must be able to record it without claiming the
    article is out there.
    """

    platform: str
    ok: bool = False
    mode: str = ""
    live: bool = True
    url: str = ""
    external_id: str = ""
    detail: str = ""
    words: int = 0
    dry_run: bool = False
    outbox_path: str = ""
    instructions: List[str] = field(default_factory=list)

    @property
    def queued(self) -> bool:
        """True when a human still has to submit this."""
        return self.ok and self.mode == MODE_MANUAL

    @property
    def state(self) -> str:
        """Ledger state for a successful attempt."""
        if self.mode == MODE_MANUAL:
            return "manual"
        return "live" if self.live else "draft"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "ok": self.ok,
            "mode": self.mode,
            "live": self.live,
            "url": self.url,
            "external_id": self.external_id,
            "detail": self.detail,
            "words": self.words,
            "dry_run": self.dry_run,
            "outbox_path": self.outbox_path,
            "instructions": list(self.instructions),
        }

    @classmethod
    def failure(cls, platform: str, detail: str, *, mode: str = "") -> "PublishOutcome":
        return cls(platform=platform, ok=False, mode=mode, live=False, detail=detail)


# --------------------------------------------------------------------------- #
# Base
# --------------------------------------------------------------------------- #


class BasePublisher:
    """Shared behaviour for every syndication target."""

    def __init__(
        self,
        config,
        *,
        dry_run: bool = False,
        platform_id: str = "",
    ) -> None:
        self.config = config
        self.dry_run = bool(dry_run)
        self.platform_id = (platform_id or self.platform).strip().lower()
        self.spec: PlatformSpec = require_platform(self.platform_id)
        self.platform = self.platform_id
        self.timeout = config.int(
            "publishing.timeout_seconds", DEFAULT_TIMEOUT, minimum=5, maximum=300
        )
        self.outbox_dir = config.path("publishing.outbox_dir", DEFAULT_OUTBOX)

    # -- configuration ----------------------------------------------------- #

    def section(self, key: str, default: Any = "") -> Any:
        """Read a value from this platform's own config block."""
        return self.config.get(f"platforms.{self.platform_id}.{key}", default)

    def opt_str(self, key: str, default: str = "") -> str:
        value = self.section(key, default)
        return str(value).strip() if value is not None else default

    def opt_int(self, key: str, default: int, *, minimum: int = 0, maximum: int = 0) -> int:
        try:
            number = int(self.section(key, default))
        except (TypeError, ValueError):
            number = default
        if number < minimum:
            return minimum
        if maximum and number > maximum:
            return maximum
        return number

    def opt_bool(self, key: str, default: bool = False) -> bool:
        value = self.section(key, default)
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "yes", "y", "on", "1"}:
            return True
        if text in {"false", "no", "n", "off", "0", ""}:
            return False
        return default

    def opt_list(self, key: str) -> List[str]:
        value = self.section(key, [])
        if isinstance(value, str):
            return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @property
    def token_envs(self) -> List[str]:
        """Candidate environment variables holding this platform's credential.

        Most targets need exactly one. WordPress needs either an application
        password or an OAuth token, so it overrides this to list both - the
        order is the preference order.
        """
        return [name for name in (self.opt_str("api_key_env"), self.spec.token_env) if name]

    @property
    def token_env(self) -> str:
        """The primary credential variable, for messages and `describe()`."""
        names = self.token_envs
        return names[0] if names else ""

    @property
    def token(self) -> str:
        """First credential that resolves from the environment. Never logged."""
        from config_loader import resolve_secret

        for name in self.token_envs:
            value = resolve_secret(name)
            if value:
                return value
        return ""

    @property
    def token_set(self) -> bool:
        return bool(self.token) or not self.token_envs

    @property
    def enabled(self) -> bool:
        return self.opt_bool("enabled", True)

    @property
    def mode(self) -> str:
        """Execution mode. `dry_run` always wins over the configured mode."""
        if self.dry_run:
            return MODE_DRY_RUN
        if self.spec.kind == "webhook":
            return MODE_WEBHOOK
        if self.spec.kind == "manual":
            return MODE_MANUAL
        return MODE_API

    # -- contract ---------------------------------------------------------- #

    def check_ready(self) -> tuple[bool, str]:
        """Is this platform able to run right now?

        The base implementation only checks what is common to every target;
        subclasses add their own credential and endpoint requirements.
        """
        return True, "ok"

    def can_publish(self, item: ContentItem) -> tuple[bool, str]:
        """Pre-flight gate, checked before any payload is built."""
        if not item.title.strip():
            return False, "article has no title"
        if not item.body_markdown.strip():
            return False, "article has no body"
        if self.platform_id not in item.platforms:
            return False, f"article does not target {self.platform_id}"
        if not self.enabled:
            return False, f"platforms.{self.platform_id}.enabled is false"
        if self.dry_run:
            return True, "dry-run"
        ready, reason = self.check_ready()
        return (True, "ok") if ready else (False, reason)

    def publish(self, item: ContentItem, payload: Dict[str, Any]) -> PublishOutcome:
        """Send one article. Subclasses implement `_deliver` / `_prepare`."""
        allowed, reason = self.can_publish(item)
        if not allowed:
            LOG.error("Cannot publish %s to %s: %s", item.id, self.platform_id, reason)
            return PublishOutcome.failure(self.platform_id, reason, mode=self.mode)

        words = int(payload.get("words") or 0)
        if self.mode == MODE_DRY_RUN:
            return PublishOutcome(
                platform=self.platform_id,
                ok=True,
                mode=MODE_DRY_RUN,
                live=False,
                detail="dry run - nothing transmitted",
                words=words,
                dry_run=True,
            )
        if self.spec.kind == "manual":
            return self._prepare(item, payload)
        try:
            return self._deliver(item, payload)
        except PublishError as exc:
            LOG.error("%s publish failed for %s: %s", self.platform_id, item.id, exc)
            return PublishOutcome.failure(self.platform_id, str(exc), mode=self.mode)

    def _deliver(self, item: ContentItem, payload: Dict[str, Any]) -> PublishOutcome:
        raise NotImplementedError

    # -- HTTP -------------------------------------------------------------- #

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        label: str = "",
    ) -> requests.Response:
        """One HTTP call with the project's error mapping and a single retry.

        Raises `PublishError` for anything the caller cannot act on. The retry
        covers only transient conditions (429/5xx) and honours `Retry-After`
        when the server sends it, so a rate-limited platform backs off instead
        of being hammered on the next tick.
        """
        request_headers = {"Accept": "application/json", "User-Agent": self.user_agent()}
        request_headers.update(headers or {})
        what = label or f"{method} {url}"

        for attempt in (1, 2):
            try:
                response = requests.request(
                    method,
                    url,
                    json=json_body,
                    data=data,
                    params=params,
                    headers=request_headers,
                    timeout=self.timeout,
                )
            except requests.Timeout as exc:
                raise PublishError(f"{what}: timed out after {self.timeout}s") from exc
            except requests.RequestException as exc:
                raise PublishError(f"{what}: {type(exc).__name__}: {exc}") from exc

            if response.status_code in RETRY_STATUSES and attempt == 1:
                delay = _retry_after(response)
                LOG.warning(
                    "%s returned HTTP %s; retrying in %ss", what, response.status_code, delay
                )
                time.sleep(delay)
                continue

            if response.status_code >= 400:
                raise PublishError(
                    f"{what}: HTTP {response.status_code} {_body_excerpt(response)}"
                )
            return response
        raise PublishError(f"{what}: still failing after retry")

    @staticmethod
    def user_agent() -> str:
        return "OutreachWizard/2.0 (+content syndication)"

    @staticmethod
    def _json(response: requests.Response) -> Dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {"data": payload}

    # -- manual hand-off --------------------------------------------------- #

    def _prepare(self, item: ContentItem, payload: Dict[str, Any]) -> PublishOutcome:
        """Hand a finished article to a human. Base implementation."""
        title = str(payload.get("title") or item.title)
        body = str(payload.get("body") or item.body_markdown)
        tags = payload.get("tags") or item.tags
        front_matter = self._front_matter(payload, tags)
        path = self.write_outbox(item.id, title, front_matter + body)
        instructions = self.submission_instructions(item, title)
        LOG.warning(
            "MANUAL SUBMISSION REQUIRED for %s on %s - no publishing API. Copy %s",
            item.id,
            self.platform_id,
            path or "(outbox write failed)",
        )
        _print_manual(item.id, self.spec.label, title, instructions, body, path)
        return PublishOutcome(
            platform=self.platform_id,
            ok=True,
            mode=MODE_MANUAL,
            live=False,
            detail=f"prepared for manual submission ({path})" if path else "prepared for manual submission",
            words=int(payload.get("words") or 0),
            outbox_path=path or "",
            instructions=instructions,
        )

    def _front_matter(self, payload: Dict[str, Any], tags: List[str]) -> str:
        """Platform-specific header written above the markdown.

        Returned with a trailing blank line, or an empty string when the platform
        wants the raw body.
        """
        return ""

    def submission_instructions(self, item: ContentItem, title: str) -> List[str]:
        steps = [f"Open {self.spec.submission_url} and sign in."]
        if self.spec.docs_url and self.spec.docs_url != self.spec.submission_url:
            steps.append(f"Read the guidelines: {self.spec.docs_url}")
        steps.append("Paste the markdown below (title as the headline).")
        steps.append("Confirm the live URL with: content confirm " + item.id)
        return steps

    def write_outbox(self, item_id: str, title: str, text: str) -> str:
        """Persist the finished article next to the campaign. Never fatal."""
        directory = self.outbox_dir / self.platform_id
        try:
            directory.mkdir(parents=True, exist_ok=True)
            safe = _SLUG_UNSAFE_RE.sub("-", item_id).strip("-") or "post"
            path = directory / f"{safe}.md"
            header = (
                f"<!-- platform: {self.spec.label} ({self.platform_id}) -->\n"
                f"<!-- item: {item_id} -->\n"
                f"<!-- title: {title} -->\n"
                f"<!-- submit at: {self.spec.submission_url} -->\n\n"
            )
            path.write_text(header + text.rstrip() + "\n", encoding="utf-8")
            return str(path)
        except OSError as exc:
            LOG.warning("Could not write content outbox %s: %s", directory, exc)
            return ""

    # -- reporting --------------------------------------------------------- #

    def describe(self) -> Dict[str, Any]:
        return {
            "platform": self.platform_id,
            "label": self.spec.label,
            "kind": self.spec.kind,
            "mode": self.mode,
            "enabled": self.enabled,
            "token_env": self.token_env,
            "token_envs": self.token_envs,
            "token_set": self.token_set,
            "docs_url": self.spec.docs_url,
            "submission_url": self.spec.submission_url,
            "legacy": self.spec.legacy,
        }

    def check(self) -> Dict[str, Any]:
        """Non-destructive readiness probe used by `platforms` and the UI.

        Readiness deliberately ignores `dry_run`: a rehearsal that reports
        "ready" when the real credential is missing is worse than useless,
        because the missing thing is exactly what the operator needs to see.
        """
        ready, reason = self.check_ready()
        described = self.describe()
        described["ready"] = ready
        described["reason"] = reason
        described["dry_run"] = self.dry_run
        described["notes"] = self.spec.notes
        return described


# --------------------------------------------------------------------------- #
# Helpers shared by subclasses
# --------------------------------------------------------------------------- #


def _retry_after(response: requests.Response) -> int:
    raw = response.headers.get("Retry-After", "")
    try:
        return max(min(int(raw), 30), 1)
    except (TypeError, ValueError):
        return 5


def _body_excerpt(response: requests.Response, limit: int = 300) -> str:
    """A short, safe slice of an error body for the log."""
    try:
        text = (response.text or "").strip()
    except Exception:  # noqa: BLE001 - a broken body must not mask the status
        return "(no body)"
    text = " ".join(text.split())
    if not text:
        return "(no body)"
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _print_manual(
    item_id: str,
    label: str,
    title: str,
    instructions: List[str],
    body: str,
    path: str,
) -> None:
    banner = "=" * 72
    print(banner)
    print(f"MANUAL POST -> {item_id}  [{label}]")
    print(f"Title : {title}")
    if path:
        print(f"Saved : {path}")
    for index, step in enumerate(instructions, start=1):
        print(f"  {index}. {step}")
    print("-" * 72)
    print(body)
    print(banner)
