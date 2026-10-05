"""Generic webhook publisher - the escape hatch.

Two of the requested communities have no API. Rather than hard-coding a list of
endpoints that will age badly, this target POSTs the whole article to a URL you
control. That covers Discourse, Ghost, Zuplo, a bespoke CMS, or an internal
queue that a human reviews - anything that accepts JSON.

The payload is deliberately self-describing and contains no secrets:

```json
{
  "item":     { "id", "title", "summary", "tags", "canonical_url", "words", "fingerprint" },
  "markdown": "<the article body>",
  "platforms": ["devto", "hashnode"],
  "source":   {"plugin": "...", "repo_url": "..."},
  "dry_run":  false
}
```

Point `platforms.webhook.url_env` at an environment variable holding the
target URL and add whatever auth header the endpoint needs via
`platforms.webhook.auth_header` / `auth_scheme`. Off by default.

This is a trust boundary: the URL is operator-supplied and the body is your own
article, so there is no injection risk - but do point it somewhere you trust with
your content.
"""

from __future__ import annotations

from typing import Any, Dict

from logging_setup import get_logger
from models import ContentItem
from publishers.base import BasePublisher, PublishOutcome

LOG = get_logger("publisher-webhook")


class WebhookPublisher(BasePublisher):
    platform = "webhook"

    def __init__(self, config, *, dry_run: bool = False) -> None:
        super().__init__(config, dry_run=dry_run, platform_id=self.platform)
        self.auth_header = self.opt_str("auth_header", "Authorization")
        self.auth_scheme = self.opt_str("auth_scheme", "Bearer")
        self.method = self.opt_str("method", "POST").upper()
        self.include_markdown = self.opt_bool("include_markdown", True)

    # -- configuration ----------------------------------------------------- #

    @property
    def url(self) -> str:
        from config_loader import resolve_secret

        explicit = self.opt_str("url")
        if explicit:
            return explicit
        return resolve_secret(self.token_env or "WEBHOOK_URL")

    def check_ready(self) -> tuple[bool, str]:
        url = self.url
        if not url:
            return False, f"platforms.webhook.url is empty and {self.token_env} is not set"
        if not url.startswith("https://"):
            return False, f"platforms.webhook URL must be https (got {url[:40]!r})"
        if self.method not in {"POST", "PUT"}:
            return False, f"platforms.webhook.method must be POST or PUT (got {self.method})"
        return True, "ok"

    def headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = self.token
        if token and self.auth_header:
            scheme = self.auth_scheme or ""
            headers[self.auth_header] = f"{scheme} {token}".strip() if scheme else token
        return headers

    # -- payload ----------------------------------------------------------- #

    def build_payload(self, item: ContentItem, payload: Dict[str, Any]) -> Dict[str, Any]:
        plugin = {
            "name": self.config.str("plugin.name"),
            "repo_url": self.config.str("plugin.repo_url"),
        }
        body: Dict[str, Any] = {
            "item": {
                "id": item.id,
                "title": payload.get("title") or item.title,
                "summary": payload.get("summary") or item.summary,
                "tags": list(payload.get("tags") or item.tags),
                "canonical_url": payload.get("canonical_url") or item.canonical_url,
                "words": int(payload.get("words") or 0),
                "fingerprint": item.fingerprint,
            },
            "platforms": list(item.platforms),
            "source": plugin,
        }
        if self.include_markdown:
            body["markdown"] = payload.get("body") or item.body_markdown
        return body

    # -- delivery ---------------------------------------------------------- #

    def _deliver(self, item: ContentItem, payload: Dict[str, Any]) -> PublishOutcome:
        body = self.build_payload(item, payload)
        response = self._request(
            self.method,
            self.url,
            json_body=body,
            headers=self.headers(),
            label=f"webhook publish {item.id}",
        )
        parsed = self._json(response)
        url = str(
            parsed.get("url")
            or parsed.get("location")
            or parsed.get("permalink")
            or response.headers.get("Location", "")
        )
        LOG.info("Webhook accepted %s | %s", item.id, url or "(no url returned)")
        return PublishOutcome(
            platform=self.platform,
            ok=True,
            mode=self.mode,
            live=bool(url),
            url=url,
            external_id=str(parsed.get("id") or ""),
            detail="accepted" if url else "accepted (endpoint returned no url)",
            words=int(payload.get("words") or 0),
        )

    def check(self) -> Dict[str, Any]:
        described = super().check()
        described["url"] = self.url
        described["method"] = self.method
        described["auth_header"] = self.auth_header
        described["include_markdown"] = self.include_markdown
        return described


def build(config, *, dry_run: bool = False) -> WebhookPublisher:
    """Entry point used by the publisher factory."""
    return WebhookPublisher(config, dry_run=dry_run)

