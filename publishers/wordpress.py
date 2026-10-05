"""WordPress publisher - WordPress.com and self-hosted.

Two flavours behind one target, because both are "the WordPress REST API" with a
different base URL and auth story:

``flavor: wordpress_com`` (default)
    ``POST https://public-api.wordpress.com/rest/v1.1/sites/<site>/posts/new``
    where ``<site>`` is a site id or domain. Authenticate with either an OAuth2
    bearer token (``auth_mode: oauth``, ``platforms.wordpress.token_env``) or HTTP
    Basic with a username plus an **application password** (``auth_mode:
    application_password``). WordPress.com also accepts Basic auth on the
    ``wp/v2`` surface; v1.1 accepts it on ``/rest/v1.1`` too, which is what is
    used here because it is the endpoint that accepts ``status``, ``categories``
    and ``tags`` as plain comma-separated names without pre-creating taxonomy
    terms.

``flavor: self_hosted``
    ``POST https://<site>/wp-json/wp/v2/posts`` with Basic auth and an
    application password from Users -> Profile -> Application Passwords.

Body is HTML: markdown is converted by `publishers.markdown_html`.

Your own site is the natural canonical home, so publish here first and point the
other platforms' ``canonical_base_url`` at the resulting permalink.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Tuple

from logging_setup import get_logger
from models import ContentItem
from publishers.base import BasePublisher, PublishError, PublishOutcome
from publishers.markdown_html import markdown_to_html

LOG = get_logger("publisher-wp")

WPCOM_API_BASE = "https://public-api.wordpress.com/rest/v1.1"
FLAVOR_WPCOM = "wordpress_com"
FLAVOR_SELF_HOSTED = "self_hosted"
VALID_STATUSES = ("publish", "draft", "pending", "private", "future")
AUTH_BASIC = "application_password"
AUTH_OAUTH = "oauth"


class WordPressPublisher(BasePublisher):
    platform = "wordpress"

    def __init__(self, config, *, dry_run: bool = False) -> None:
        super().__init__(config, dry_run=dry_run, platform_id=self.platform)
        self.flavor = self.opt_str("flavor", FLAVOR_WPCOM).lower()
        self.site = self.opt_str("site")
        self.site_url = self.opt_str("site_url").rstrip("/")
        self.username_env = self.opt_str("username_env", "WP_USERNAME")
        self.password_env = self.opt_str("password_env", "WP_APP_PASSWORD")
        self.oauth_env = self.opt_str("oauth_env", "WPCOM_OAUTH_TOKEN")
        self.status = self.opt_str("status", "publish").lower()
        if self.status not in VALID_STATUSES:
            self.status = "publish"
        self.categories = self.opt_list("categories")
        self.auth_mode = self.opt_str("auth_mode", AUTH_BASIC).lower()
        self.api_base = self.opt_str("api_base") or self._default_api_base()

    @property
    def token_envs(self) -> List[str]:
        """Both credential shapes are legitimate, so accept either."""
        if self.auth_mode == AUTH_OAUTH:
            order = (self.oauth_env,)
        else:
            order = (self.password_env, self.oauth_env)
        fallback = (self.opt_str("api_key_env"), self.spec.token_env)
        return [name for name in (*order, *fallback) if name]

    @property
    def username(self) -> str:
        """Configured literal, else the value of `username_env`."""
        from config_loader import resolve_secret

        literal = self.opt_str("username")
        return literal or resolve_secret(self.username_env)

    def _default_api_base(self) -> str:
        if self.flavor == FLAVOR_SELF_HOSTED:
            return f"{self.site_url}/wp-json/wp/v2" if self.site_url else ""
        return WPCOM_API_BASE

    # -- configuration ----------------------------------------------------- #

    def check_ready(self) -> tuple[bool, str]:
        if self.flavor not in {FLAVOR_WPCOM, FLAVOR_SELF_HOSTED}:
            return False, f"platforms.wordpress.flavor must be {FLAVOR_WPCOM} or {FLAVOR_SELF_HOSTED}"
        if not self.api_base.startswith("https://"):
            return False, f"platforms.wordpress.api_base must be https (got {self.api_base!r})"
        if self.auth_mode not in {AUTH_BASIC, AUTH_OAUTH}:
            return False, f"platforms.wordpress.auth_mode must be {AUTH_BASIC} or {AUTH_OAUTH}"
        if self.flavor == FLAVOR_WPCOM and not self.site:
            return False, "platforms.wordpress.site must be a WordPress.com site id or domain"
        if self.flavor == FLAVOR_SELF_HOSTED and not self.site_url:
            return False, "platforms.wordpress.site_url is required for the self_hosted flavor"
        if self.auth_mode == AUTH_OAUTH:
            return True, "ok"
        if not self.username:
            return (
                False,
                f"set platforms.wordpress.username or environment variable {self.username_env}",
            )
        if not self.token:
            return (
                False,
                f"environment variable {self.password_env} is not set "
                "(create one under Users -> Profile -> Application Passwords)",
            )
        return True, "ok"

    def endpoint(self) -> str:
        if self.flavor == FLAVOR_SELF_HOSTED:
            return f"{self.api_base}/posts"
        return f"{self.api_base}/sites/{self.site}/posts/new"

    def auth_headers(self) -> Dict[str, str]:
        from config_loader import resolve_secret

        if self.auth_mode == AUTH_OAUTH:
            token = resolve_secret(self.oauth_env) or self.token
            return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        raw = f"{self.username}:{self.token}".encode("utf-8")
        return {
            "Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
            "Content-Type": "application/json",
        }

    # -- payload ----------------------------------------------------------- #

    def build_post(self, item: ContentItem, payload: Dict[str, Any]) -> Dict[str, Any]:
        html_body = markdown_to_html(str(payload.get("body") or item.body_markdown))
        post: Dict[str, Any] = {
            "title": payload.get("title") or item.title,
            "content": html_body,
            "status": self.status,
        }
        summary = payload.get("summary") or item.summary
        if summary:
            post["excerpt"] = summary
        tags = list(payload.get("tags") or item.tags)[: self.spec.tag_limit]
        if tags and self.spec.supports_tags:
            post["tags"] = ",".join(tags)
        if self.categories and self.spec.supports_categories:
            post["categories"] = ",".join(self.categories)
        return post

    # -- delivery ---------------------------------------------------------- #

    def _deliver(self, item: ContentItem, payload: Dict[str, Any]) -> PublishOutcome:
        post = self.build_post(item, payload)
        response = self._request(
            "POST",
            self.endpoint(),
            json_body=post,
            headers=self.auth_headers(),
            label=f"wordpress publish {item.id}",
        )
        url, external_id = _extract_url_and_id(self._json(response))
        if not url:
            raise PublishError("wordpress accepted the post but returned no link")
        live = self.status == "publish"
        LOG.info(
            "Sent %s to wordpress (%s/%s) | %s",
            item.id,
            self.flavor,
            self.status,
            url,
        )
        return PublishOutcome(
            platform=self.platform,
            ok=True,
            mode=self.mode,
            live=live,
            url=url,
            external_id=external_id,
            detail=f"status={self.status}",
            words=int(payload.get("words") or 0),
        )

    def check(self) -> Dict[str, Any]:
        described = super().check()
        described["flavor"] = self.flavor
        described["site"] = self.site
        described["site_url"] = self.site_url
        described["api_base"] = self.api_base
        described["endpoint"] = self.endpoint() if self.api_base else ""
        described["auth_mode"] = self.auth_mode
        described["status"] = self.status
        described["categories"] = self.categories
        return described


def _extract_url_and_id(body: Dict[str, Any]) -> Tuple[str, str]:
    """Pull the permalink out of either response shape.

    WordPress.com v1.1 returns ``ID`` + ``URL``; ``wp/v2`` returns ``id`` +
    ``link``. Check both rather than assuming.
    """
    url = str(body.get("URL") or body.get("url") or body.get("link") or "")
    external_id = str(body.get("ID") or body.get("id") or "")
    return url, external_id


def build(config, *, dry_run: bool = False) -> WordPressPublisher:
    """Entry point used by the publisher factory."""
    return WordPressPublisher(config, dry_run=dry_run)

