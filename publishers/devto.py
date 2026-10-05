"""DEV Community (dev.to) publisher - Forem v1 API.

`POST https://dev.to/api/articles` with two headers that are both mandatory:
the `api-key` from Settings -> Extensions, and `accept:
application/vnd.forem.api-v1+json` (without the accept header Forem silently
routes the call to the deprecated v0 surface).

The payload is nested under an `article` object. Three fields carry real
decisions:

* `tags` - DEV accepts at most 4, lowercase, no spaces. Extras are trimmed by
  `content_engine.normalise_tags` before we get here.
* `canonical_url` - points at wherever the article's primary home is. Setting it
  tells DEV the copy here is the syndication, which is what keeps a
  cross-posted article from being treated as duplicate content.
* `published` - `false` creates a draft in the dashboard, which is the safe
  default for a first run. Set `platforms.devto.state_published: true` when you
  are deliberately syndicating.
"""

from __future__ import annotations

from typing import Any, Dict

from logging_setup import get_logger
from models import ContentItem
from publishers.base import BasePublisher, PublishError, PublishOutcome

LOG = get_logger("publisher-devto")

DEFAULT_API_BASE = "https://dev.to/api"
FOREM_ACCEPT = "application/vnd.forem.api-v1+json"


class DevToPublisher(BasePublisher):
    platform = "devto"

    def __init__(self, config, *, dry_run: bool = False) -> None:
        super().__init__(config, dry_run=dry_run, platform_id=self.platform)
        self.api_base = self.opt_str("api_base", DEFAULT_API_BASE).rstrip("/")
        self.organization = self.opt_str("organization_username")
        self.state_published = self.opt_bool("state_published", False)
        self.series = self.opt_str("series")

    # -- configuration ----------------------------------------------------- #

    def check_ready(self) -> tuple[bool, str]:
        if not self.token:
            return False, f"environment variable {self.token_env} is not set"
        if not self.api_base.startswith("https://"):
            return False, f"platforms.devto.api_base must be https (got {self.api_base!r})"
        return True, "ok"

    # -- payload ----------------------------------------------------------- #

    def build_article(self, item: ContentItem, payload: Dict[str, Any]) -> Dict[str, Any]:
        article: Dict[str, Any] = {
            "title": payload.get("title") or item.title,
            "body_markdown": payload.get("body") or item.body_markdown,
            "published": bool(self.state_published),
            "tags": list(payload.get("tags") or item.tags)[: self.spec.tag_limit],
        }
        summary = payload.get("summary") or item.summary
        if summary:
            article["description"] = str(summary)[:300]
        canonical = payload.get("canonical_url") or item.canonical_url
        if canonical and self.spec.supports_canonical:
            article["canonical_url"] = canonical
        if self.organization:
            article["organization_username"] = self.organization
        if self.series:
            article["series"] = self.series
        return article

    # -- delivery ---------------------------------------------------------- #

    def _deliver(self, item: ContentItem, payload: Dict[str, Any]) -> PublishOutcome:
        article = self.build_article(item, payload)
        response = self._request(
            "POST",
            f"{self.api_base}/articles",
            json_body={"article": article},
            headers={"api-key": self.token, "accept": FOREM_ACCEPT, "Content-Type": "application/json"},
            label=f"dev.to publish {item.id}",
        )
        body = self._json(response)
        if not body:
            raise PublishError("dev.to returned an empty body on a successful create")
        url = str(body.get("url") or "")
        if not url:
            raise PublishError("dev.to accepted the article but returned no url")
        LOG.info(
            "Published %s on dev.to%s | %s",
            item.id,
            " (draft)" if not self.state_published else "",
            url,
        )
        return PublishOutcome(
            platform=self.platform,
            ok=True,
            mode=self.mode,
            live=bool(self.state_published),
            url=url,
            external_id=str(body.get("id") or ""),
            detail="draft created" if not self.state_published else "published",
            words=int(payload.get("words") or 0),
        )

    def check(self) -> Dict[str, Any]:
        described = super().check()
        described["organization"] = self.organization
        described["state_published"] = self.state_published
        described["api_base"] = self.api_base
        described["tag_limit"] = self.spec.tag_limit
        return described


def build(config, *, dry_run: bool = False) -> DevToPublisher:
    """Entry point used by the publisher factory."""
    return DevToPublisher(config, dry_run=dry_run)

