"""Hashnode publisher - GraphQL at https://gql.hashnode.com.

Hashnode is the one target that is not REST. A publish is a single GraphQL
mutation, `publishPost`, carrying markdown directly - no conversion needed.

Two things bite people here and both are handled explicitly:

* **A publication id is mandatory.** `publicationId` is an object id, not a
  subdomain. `discover_publications()` resolves it from the token when the config
  leaves it blank, and `python main.py platforms` shows what was found.
* **Write mutations are Pro-gated.** A free publication answers `FORBIDDEN` with
  a message about the Pro plan. That is a configuration limitation, not a bug,
  so it is surfaced verbatim rather than retried.

`originalArticleURL` is Hashnode's canonical field and should point at the
article's primary home to avoid the same duplicate-content problem DEV has.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from logging_setup import get_logger
from models import ContentItem
from publishers.base import BasePublisher, PublishError, PublishOutcome

LOG = get_logger("publisher-hashnode")

DEFAULT_API_BASE = "https://gql.hashnode.com"

PUBLISH_MUTATION = """
mutation PublishPost($input: PublishPostInput!) {
  publishPost(input: $input) {
    post { id slug url title }
  }
}
"""

CREATE_DRAFT_MUTATION = """
mutation CreateDraft($input: CreateDraftInput!) {
  createDraft(input: $input) { draft { id title slug } }
}
"""

ME_QUERY = """
query Me {
  me {
    username
    publications(first: 10) { edges { node { id title url } } }
  }
}
"""


class HashnodePublisher(BasePublisher):
    platform = "hashnode"

    def __init__(self, config, *, dry_run: bool = False) -> None:
        super().__init__(config, dry_run=dry_run, platform_id=self.platform)
        self.api_base = self.opt_str("api_base", DEFAULT_API_BASE)
        self.publication_id = self.opt_str("publication_id")
        self.enable_toc = self.opt_bool("enable_toc", True)
        self.subtitle = self.opt_str("subtitle")
        self.publish_immediately = self.opt_bool("publish_immediately", True)

    # -- configuration ----------------------------------------------------- #

    def check_ready(self) -> tuple[bool, str]:
        if not self.token:
            return False, f"environment variable {self.token_env} is not set"
        if not self.api_base.startswith("https://"):
            return False, f"platforms.hashnode.api_base must be https (got {self.api_base!r})"
        if not self.publication_id:
            return (
                False,
                "platforms.hashnode.publication_id is empty - run "
                "'python main.py platforms --probe hashnode' to list yours",
            )
        return True, "ok"

    def graphql(self, query: str, variables: Dict[str, Any], *, label: str) -> Dict[str, Any]:
        """One GraphQL call. Raises `PublishError` with the server's own message."""
        response = self._request(
            "POST",
            self.api_base,
            json_body={"query": query, "variables": variables},
            headers={
                "Content-Type": "application/json",
                "Authorization": self.token,
                "accept": "application/json",
            },
            label=label,
        )
        body = self._json(response)
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            message = str(first.get("message") or errors[0])
            extensions = first.get("extensions") if isinstance(first, dict) else {}
            code = str((extensions or {}).get("code") or "")
            if code.upper() == "FORBIDDEN" or "pro plan" in message.lower():
                message = (
                    f"{message} (Hashnode gates write mutations on a Pro plan - "
                    "either upgrade the publication or use "
                    "platforms.hashnode.publication_id for a Pro one.)"
                )
            raise PublishError(f"{label}: {message}")
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def discover_publications(self) -> List[Dict[str, str]]:
        """List the publications this token can write to.

        Used by `platforms --probe`; safe to call because it is a read.
        """
        if not self.token:
            return []
        data = self.graphql(ME_QUERY, {}, label="hashnode me")
        me = data.get("me") or {}
        edges = ((me.get("publications") or {}).get("edges")) or []
        found: List[Dict[str, str]] = []
        for edge in edges:
            node = (edge or {}).get("node") or {}
            if node.get("id"):
                found.append(
                    {
                        "id": str(node.get("id")),
                        "title": str(node.get("title") or ""),
                        "url": str(node.get("url") or ""),
                    }
                )
        return found

    # -- payload ----------------------------------------------------------- #

    def build_input(self, item: ContentItem, payload: Dict[str, Any]) -> Dict[str, Any]:
        post_input: Dict[str, Any] = {
            "publicationId": self.publication_id,
            "title": payload.get("title") or item.title,
            "contentMarkdown": payload.get("body") or item.body_markdown,
        }
        tags = list(payload.get("tags") or item.tags)[: self.spec.tag_limit]
        if tags:
            post_input["tags"] = [{"slug": tag} for tag in tags]
        canonical = payload.get("canonical_url") or item.canonical_url
        if canonical and self.spec.supports_canonical:
            post_input["originalArticleURL"] = canonical
        if self.subtitle:
            post_input["subtitle"] = self.subtitle
        if self.enable_toc:
            post_input["enableToc"] = True
        return post_input

    # -- delivery ---------------------------------------------------------- #

    def _deliver(self, item: ContentItem, payload: Dict[str, Any]) -> PublishOutcome:
        post_input = self.build_input(item, payload)
        if self.publish_immediately:
            data = self.graphql(
                PUBLISH_MUTATION, {"input": post_input}, label=f"hashnode publish {item.id}"
            )
            post = ((data.get("publishPost") or {}).get("post")) or {}
            url = str(post.get("url") or "")
            external_id = str(post.get("id") or "")
            live = True
            detail = "published"
        else:
            data = self.graphql(
                CREATE_DRAFT_MUTATION, {"input": post_input}, label=f"hashnode draft {item.id}"
            )
            draft = ((data.get("createDraft") or {}).get("draft")) or {}
            url = ""
            external_id = str(draft.get("id") or "")
            live = False
            detail = "draft created - publish it from the Hashnode dashboard"
            if not external_id:
                raise PublishError("hashnode accepted the draft but returned no id")

        LOG.info("Sent %s to hashnode | %s", item.id, url or detail)
        return PublishOutcome(
            platform=self.platform,
            ok=True,
            mode=self.mode,
            live=live,
            url=url,
            external_id=external_id,
            detail=detail,
            words=int(payload.get("words") or 0),
        )

    def check(self) -> Dict[str, Any]:
        described = super().check()
        described["api_base"] = self.api_base
        described["publication_id"] = self.publication_id
        described["publish_immediately"] = self.publish_immediately
        described["enable_toc"] = self.enable_toc
        return described


def publication_hint(found: List[Dict[str, str]]) -> Optional[str]:
    """First publication id from a probe, for the operator's convenience."""
    return found[0]["id"] if found else None


def build(config, *, dry_run: bool = False) -> HashnodePublisher:
    """Entry point used by the publisher factory."""
    return HashnodePublisher(config, dry_run=dry_run)

