"""Medium publisher - legacy OAuth2 API.

**Medium no longer issues integration tokens.** The official help centre states
plainly that no new integration tokens are granted and no new integrations are
accepted; the API documentation repository is archived with "The Medium API is no
longer supported." Tokens issued before 2025-01-01 still work.

That makes this target a legacy integration rather than a dead one, so it is
implemented properly and shipped **disabled by default** (`platforms.medium.enabled:
false`). Enable it only if you already hold a token; otherwise use manual mode,
which hands you a paste-ready draft.

Endpoint shape (unchanged since 2020):

* `POST /v1/users/{userId}/posts`   - publish on the authenticated profile
* `POST /v1/publications/{pubId}/posts` - publish into a publication

Both take JSON with `title`, `contentFormat` (`html` or `markdown`), `body`,
`tags`, `publishStatus` (`draft`, `public`, `unlisted`) and `license`.
"""

from __future__ import annotations

from typing import Any, Dict

from logging_setup import get_logger
from models import ContentItem
from publishers.base import BasePublisher, PublishError, PublishOutcome
from publishers.markdown_html import markdown_to_html

LOG = get_logger("publisher-medium")

DEFAULT_API_BASE = "https://api.medium.com/v1"
LICENSE_URL = "https://medium.com/policy/9db0094a1e0f"
VALID_CONTENT_FORMATS = ("html", "markdown")
VALID_PUBLISH_STATUS = ("draft", "public", "unlisted")


class MediumPublisher(BasePublisher):
    platform = "medium"

    def __init__(self, config, *, dry_run: bool = False) -> None:
        super().__init__(config, dry_run=dry_run, platform_id=self.platform)
        self.api_base = self.opt_str("api_base", DEFAULT_API_BASE).rstrip("/")
        self.author_id = self.opt_str("author_id")
        self.publication_id = self.opt_str("publication_id")
        self.content_format = self.opt_str("content_format", "html").lower()
        if self.content_format not in VALID_CONTENT_FORMATS:
            self.content_format = "html"
        self.publish_status = self.opt_str("publish_status", "public").lower()
        if self.publish_status not in VALID_PUBLISH_STATUS:
            self.publish_status = "public"
        self.license = self.opt_str("license", "all-rights-reserved")

    # -- configuration ----------------------------------------------------- #

    def check_ready(self) -> tuple[bool, str]:
        if not self.token:
            return (
                False,
                f"environment variable {self.token_env} is not set - and Medium no longer "
                "issues integration tokens, so this only works with a pre-2025 token",
            )
        if not self.api_base.startswith("https://"):
            return False, f"platforms.medium.api_base must be https (got {self.api_base!r})"
        if not self.author_id and not self.publication_id:
            return (
                False,
                "set platforms.medium.author_id (profile post) or "
                "platforms.medium.publication_id (publication post)",
            )
        return True, "ok"

    def endpoint(self) -> str:
        if self.publication_id:
            return f"{self.api_base}/publications/{self.publication_id}/posts"
        return f"{self.api_base}/users/{self.author_id}/posts"

    def fetch_identity(self) -> Dict[str, str]:
        """`GET /v1/me` - resolves the author id when config left it blank."""
        if not self.token:
            return {}
        response = self._request(
            "GET",
            f"{self.api_base}/me",
            headers={"Authorization": f"Bearer {self.token}"},
            label="medium me",
        )
        body = self._json(response)
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        return {
            "id": str(data.get("id") or ""),
            "username": str(data.get("username") or ""),
        }

    # -- payload ----------------------------------------------------------- #

    def build_body(self, item: ContentItem, payload: Dict[str, Any]) -> str:
        markdown = str(payload.get("body") or item.body_markdown)
        if self.content_format == "markdown":
            return markdown
        return markdown_to_html(markdown)

    def build_post(self, item: ContentItem, payload: Dict[str, Any]) -> Dict[str, Any]:
        post: Dict[str, Any] = {
            "title": payload.get("title") or item.title,
            "contentFormat": self.content_format,
            "body": self.build_body(item, payload),
            "publishStatus": self.publish_status,
            "license": self.license,
            "licenseUrl": LICENSE_URL,
        }
        tags = list(payload.get("tags") or item.tags)[: self.spec.tag_limit]
        if tags and self.spec.supports_tags:
            post["tags"] = tags
        canonical = payload.get("canonical_url") or item.canonical_url
        if canonical:
            post["canonicalUrl"] = canonical
        return post

    # -- delivery ---------------------------------------------------------- #

    def _deliver(self, item: ContentItem, payload: Dict[str, Any]) -> PublishOutcome:
        post = self.build_post(item, payload)
        response = self._request(
            "POST",
            self.endpoint(),
            json_body=post,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            label=f"medium publish {item.id}",
        )
        body = self._json(response)
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        url = str(data.get("url") or "")
        if not url:
            raise PublishError("medium accepted the post but returned no url")
        live = self.publish_status == "public"
        LOG.info("Sent %s to medium | %s (%s)", item.id, url, self.publish_status)
        return PublishOutcome(
            platform=self.platform,
            ok=True,
            mode=self.mode,
            live=live,
            url=url,
            external_id=str(data.get("id") or ""),
            detail=f"publishStatus={self.publish_status}",
            words=int(payload.get("words") or 0),
        )

    def check(self) -> Dict[str, Any]:
        described = super().check()
        described["api_base"] = self.api_base
        described["author_id"] = self.author_id
        described["publication_id"] = self.publication_id
        described["content_format"] = self.content_format
        described["publish_status"] = self.publish_status
        return described


def build(config, *, dry_run: bool = False) -> MediumPublisher:
    """Entry point used by the publisher factory."""
    return MediumPublisher(config, dry_run=dry_run)

