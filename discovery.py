"""GitHub sponsor discovery.

Scans the Genesys Cloud topics on GitHub, groups repositories by owner, ranks
the owners that look like *active builders* (the ones most likely to sponsor
or co-market a plugin), and optionally locates a public contact address from
the owner profile or their public website.

No scraping of private data: only the public REST API and the owner's own
published website. Contacts are surfaced for a human to confirm before use.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # pragma: no cover - import guard
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "The 'requests' package is required. Run: pip install -r requirements.txt"
    ) from exc

from logging_setup import get_logger
from models import (
    OWNER_USER,
    DiscoveredSponsor,
    is_valid_email,
    looks_like_noise_email,
    normalize_name,
)

LOG = get_logger("discovery")

DEFAULT_API_BASE = "https://api.github.com"
_EMAIL_IN_TEXT = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}",
    re.IGNORECASE,
)
_JUNK_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".woff", ".woff2")
_NON_HTML_SUFFIXES = (".pdf", ".zip", ".xml", ".json", ".css", ".js")


class DiscoveryError(Exception):
    """Raised when GitHub cannot be reached or the token is invalid."""


# --------------------------------------------------------------------------- #
# GitHub client
# --------------------------------------------------------------------------- #


@dataclass
class GitHubClient:
    """Thin, polite wrapper around the parts of the GitHub REST API we need."""

    token: str = ""
    api_base: str = DEFAULT_API_BASE
    user_agent: str = "GenesysPluginSponsorBot/1.0"
    timeout: int = 20
    delay: float = 0.4
    token_env: str = "GITHUB_TOKEN"
    session: Optional[requests.Session] = None

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": self.user_agent,
            }
        )
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"
        self.token_env = (self.token_env or "GITHUB_TOKEN").strip() or "GITHUB_TOKEN"
        self.api_base = self.api_base.rstrip("/")

    # -- plumbing ---------------------------------------------------------- #

    def _sleep(self) -> None:
        if self.delay > 0:
            time.sleep(self.delay)

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        self._sleep()
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DiscoveryError(f"request to {url} failed: {exc}") from exc

        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            reset = response.headers.get("X-RateLimit-Reset")
            wait = self._seconds_until(reset)
            raise DiscoveryError(
                "GitHub API rate limit exhausted "
                f"(resets in ~{wait}s). Set ${self.token_env} to raise it."
            )
        if response.status_code == 401:
            raise DiscoveryError(
                f"GitHub rejected the credentials (401). Check ${self.token_env}."
            )
        if response.status_code == 403:
            raise DiscoveryError(
                "GitHub returned 403 (rate limit or blocked). "
                "Set GITHUB_TOKEN to a personal access token and retry."
            )
        if response.status_code == 404:
            LOG.debug("404 for %s", url)
            return {}
        if response.status_code >= 400:
            raise DiscoveryError(f"GitHub returned HTTP {response.status_code} for {url}")
        try:
            return response.json()
        except ValueError as exc:
            raise DiscoveryError(f"invalid JSON from {url}: {exc}") from exc

    @staticmethod
    def _seconds_until(reset_header: Optional[str]) -> int:
        try:
            reset_at = int(reset_header or 0)
        except (TypeError, ValueError):
            return 60
        return max(0, reset_at - int(time.time()))

    @property
    def rate_limit(self) -> Dict[str, Any]:
        """Current rate-limit window (cheap, unauthenticated-friendly)."""
        url = f"{self.api_base}/rate_limit"
        try:
            payload = self.session.get(url, timeout=self.timeout).json()
        except (requests.RequestException, ValueError) as exc:
            raise DiscoveryError(f"could not read rate limit: {exc}") from exc
        return payload.get("resources", {}).get("search", {})

    # -- API surface ------------------------------------------------------- #

    def search_repositories(self, topic: str, *, per_page: int = 100, max_pages: int = 3) -> List[Dict[str, Any]]:
        """All repos tagged with ``topic`` (up to ``max_pages`` x per_page)."""
        clean_topic = topic.strip().lstrip("#")
        if not clean_topic:
            return []
        collected: List[Dict[str, Any]] = []
        for page in range(1, max(1, max_pages) + 1):
            payload = self._get(
                "/search/repositories",
                params={"q": f"topic:{clean_topic}", "per_page": per_page, "page": page},
            )
            items = payload.get("items") or []
            if not isinstance(items, list):
                break
            collected.extend(item for item in items if isinstance(item, dict))
            total = int(payload.get("total_count") or 0)
            LOG.info(
                "topic:%s page %d -> %d repos (+%d total seen, %d indexed)",
                clean_topic,
                page,
                len(items),
                len(collected),
                total,
            )
            if len(items) < per_page or len(collected) >= min(total, per_page * max_pages):
                break
        return collected

    def get_owner(self, login: str) -> Dict[str, Any]:
        return self._get(f"/users/{login}")

    def get_authenticated_user(self) -> Dict[str, Any]:
        """The token owner's profile, or ``{}`` when running anonymously.

        Used by the web UI's "connect to GitHub" step to prove a token works.
        """
        if not self.token:
            return {}
        return self._get("/user")

    def fetch_page(self, url: str, *, max_bytes: int = 400_000) -> str:
        """Fetch a public HTML page, capped in size. Best effort."""
        if not url.lower().startswith(("http://", "https://")):
            return ""
        self._sleep()
        try:
            response = self.session.get(
                url,
                timeout=min(self.timeout, 15),
                headers={"User-Agent": self.user_agent},
                allow_redirects=True,
                stream=True,
            )
            if response.status_code >= 400:
                LOG.debug("website fetch %s -> HTTP %s", url, response.status_code)
                return ""
            content = response.raw.read(max_bytes, decode_content=True) or b""
            return content.decode(response.encoding or "utf-8", errors="replace")
        except requests.RequestException as exc:
            LOG.debug("website fetch %s failed: %s", url, exc)
            return ""

    # -- email discovery --------------------------------------------------- #

    def find_public_email(self, owner: Dict[str, Any], *, max_sites: int = 3) -> Tuple[str, str]:
        """Best-effort public contact address. Returns ``(email, source)``."""
        api_email = (owner.get("email") or "").strip().lower()
        if api_email and is_valid_email(api_email) and not looks_like_noise_email(api_email):
            return api_email, "github-profile"

        candidates: List[str] = []
        for field in ("website", "blog"):
            value = (owner.get(field) or "").strip()
            if value:
                candidates.append(value if value.startswith("http") else f"https://{value}")
        if owner.get("twitter_username"):
            candidates.append(f"https://x.com/{owner['twitter_username']}")

        checked = 0
        for url in dict.fromkeys(candidates):
            if checked >= max_sites:
                break
            if url.rstrip("/").lower().endswith(_NON_HTML_SUFFIXES):
                continue
            checked += 1
            email = self._extract_email(self.fetch_page(url))
            if email:
                return email, url
        return "", ""

    @staticmethod
    def _extract_email(html: str) -> str:
        if not html:
            return ""
        text = html.replace("\n", " ")
        found: List[str] = []
        for match in _EMAIL_IN_TEXT.findall(text):
            email = match.strip().strip(".,;:!?'\"").lower()
            if not is_valid_email(email) or looks_like_noise_email(email):
                continue
            if any(email.endswith(extension) for extension in _JUNK_EXTENSIONS):
                continue
            if len(email) > 254 or email.count("@") != 1:
                continue
            found.append(email)
        # Prefer role/contact addresses over personal ones when both exist.
        for email in found:
            if email.split("@", 1)[0] in {"contact", "info", "hello", "sales", "partnerships", "support"}:
                return email
        return found[0] if found else ""


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


@dataclass
class _OwnerBucket:
    login: str
    owner_type: str = OWNER_USER
    repos_count: int = 0
    stars: int = 0
    forks: int = 0
    best_stars: int = -1
    top_repo: str = ""
    top_repo_url: str = ""
    description: str = ""
    language: str = ""
    repo_ids: set = None  # type: ignore[assignment]
    topics: List[str] = None  # type: ignore[assignment]
    discovered_from: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.repo_ids is None:
            self.repo_ids = set()
        if self.topics is None:
            self.topics = []
        if self.discovered_from is None:
            self.discovered_from = []

    def add_repo(self, repo: Dict[str, Any], topic: str) -> None:
        stars = int(repo.get("stargazers_count") or 0)
        repo_id = str(repo.get("id") or repo.get("full_name") or "")
        if repo_id and repo_id in self.repo_ids:
            # Same repo matched via two topics - count it once.
            if topic not in self.discovered_from:
                self.discovered_from.append(topic)
            return
        if repo_id:
            self.repo_ids.add(repo_id)
        self.repos_count += 1
        self.stars += stars
        self.forks += int(repo.get("forks_count") or 0)
        if stars > self.best_stars:
            self.best_stars = stars
            self.top_repo = str(repo.get("full_name") or "")
            self.top_repo_url = str(repo.get("html_url") or "")
            self.description = str(repo.get("description") or "")
            self.language = str(repo.get("language") or "")
        for existing in repo.get("topics") or []:
            if isinstance(existing, str) and existing not in self.topics:
                self.topics.append(existing)
        if topic not in self.discovered_from:
            self.discovered_from.append(topic)


def aggregate_repositories(repos_by_topic: Dict[str, Sequence[Dict[str, Any]]]) -> Dict[str, _OwnerBucket]:
    """Group repositories into per-owner buckets, keyed by normalized login."""
    buckets: Dict[str, _OwnerBucket] = {}
    for topic, repos in repos_by_topic.items():
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            owner = repo.get("owner") or {}
            login = str(owner.get("login") or "").strip()
            if not login:
                continue
            key = normalize_name(login)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = _OwnerBucket(login=login, owner_type=str(owner.get("type") or OWNER_USER))
                buckets[key] = bucket
            bucket.add_repo(repo, topic)
    return buckets


def passes_filter(candidate: DiscoveredSponsor, *, min_org_repos: int, min_individual_genesys_repos: int) -> bool:
    """Orgs need total repos, individuals need Genesys-topic repos."""
    if candidate.is_org:
        return candidate.repos_count >= min_org_repos
    return candidate.genesys_repos_count >= min_individual_genesys_repos


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def discover_sponsors(
    client: GitHubClient,
    topics: Iterable[str],
    *,
    min_org_repos: int = 5,
    min_individual_genesys_repos: int = 3,
    min_stars: int = 0,
    exclude_forks: bool = True,
    per_page: int = 100,
    max_pages: int = 3,
    max_owner_lookups: int = 40,
    scrape_public_emails: bool = True,
    email_scrape_max_sites: int = 3,
) -> List[DiscoveredSponsor]:
    """Run the full discovery sweep and return qualifying candidates."""
    topic_list = [topic.strip().lstrip("#") for topic in topics if topic and topic.strip()]
    if not topic_list:
        raise DiscoveryError("no topics configured (github.topics)")

    repos_by_topic: Dict[str, List[Dict[str, Any]]] = {}
    for topic in topic_list:
        LOG.info("Scanning topic:%s", topic)
        try:
            repos = client.search_repositories(topic, per_page=per_page, max_pages=max_pages)
        except DiscoveryError as exc:
            LOG.error("topic:%s failed - %s", topic, exc)
            continue
        if exclude_forks:
            repos = [repo for repo in repos if not repo.get("fork")]
        repos_by_topic[topic] = repos

    buckets = aggregate_repositories(repos_by_topic)
    LOG.info("Aggregated %d repos across %d owners", sum(len(v) for v in repos_by_topic.values()), len(buckets))

    # Build candidates that already look promising, without hitting the API yet.
    preliminary: List[DiscoveredSponsor] = []
    for bucket in buckets.values():
        if bucket.stars < min_stars:
            continue
        preliminary.append(
            DiscoveredSponsor(
                owner_name=bucket.login,
                owner_type=bucket.owner_type,
                github_url=f"https://github.com/{bucket.login}",
                repos_count=bucket.repos_count,
                genesys_repos_count=bucket.repos_count,
                top_repo=bucket.top_repo,
                top_repo_url=bucket.top_repo_url,
                stars=bucket.stars,
                forks=bucket.forks,
                description=bucket.description,
                language=bucket.language,
                topics=list(bucket.topics),
                discovered_from=list(bucket.discovered_from),
            )
        )

    preliminary.sort(key=lambda item: (-item.stars, -item.genesys_repos_count, item.owner_name.lower()))

    # Owner profile lookups give the real repo count + public email. Budgeted.
    lookups = 0
    for candidate in preliminary:
        if lookups >= max_owner_lookups:
            LOG.info("Owner lookup budget (%d) reached", max_owner_lookups)
            break
        owner = client.get_owner(candidate.owner_name)
        if not owner:
            continue
        lookups += 1
        candidate.owner_type = str(owner.get("type") or candidate.owner_type)
        candidate.repos_count = int(owner.get("public_repos") or candidate.repos_count)
        candidate.location = str(owner.get("location") or "")
        candidate.blog = str(owner.get("blog") or "")
        candidate.website = str(owner.get("website") or "")
        candidate.github_url = str(owner.get("html_url") or candidate.github_url)
        if scrape_public_emails:
            email, source = client.find_public_email(owner, max_sites=email_scrape_max_sites)
            if email:
                candidate.email_if_public = email
                candidate.email_source = source

    if len(preliminary) > lookups:
        LOG.info(
            "%d owner(s) not profiled (lookup budget %d); their org filter is evaluated "
            "against topic-tagged repos only",
            len(preliminary) - lookups,
            max_owner_lookups,
        )

    qualified = [
        candidate
        for candidate in preliminary
        if passes_filter(
            candidate,
            min_org_repos=min_org_repos,
            min_individual_genesys_repos=min_individual_genesys_repos,
        )
    ]
    qualified.sort(key=lambda item: (-item.stars, -item.genesys_repos_count, item.owner_name.lower()))
    LOG.info(
        "Discovery complete: %d/%d owners passed the filter (org>=%d repos, individual>=%d genesys repos)",
        len(qualified),
        len(preliminary),
        min_org_repos,
        min_individual_genesys_repos,
    )
    return qualified


def client_from_config(config) -> GitHubClient:  # noqa: ANN001 - avoid import cycle
    """Build a GitHubClient from a `Config` instance."""
    return GitHubClient(
        token=config.github_token,
        api_base=config.str("github.api_base", DEFAULT_API_BASE),
        user_agent=config.str("github.user_agent", "GenesysPluginSponsorBot/1.0"),
        timeout=config.int("github.timeout_seconds", 20, minimum=5, maximum=120),
        delay=config.float("github.request_delay_seconds", 0.4),
        token_env=config.str("github.token_env", "GITHUB_TOKEN"),
    )


# --------------------------------------------------------------------------- #
# CLI helpers
# --------------------------------------------------------------------------- #


def render_table(candidates: Sequence[DiscoveredSponsor], *, width: int = 3) -> str:
    """Human readable candidate listing for the CLI."""
    if not candidates:
        return "No candidates found."
    header = f"{'#':>2}  {'OWNER':<32} {'TYPE':<12} {'REPOS':>5} {'GEN':>4} {'STARS':>6}  {'EMAIL':<34} TOP REPO"
    lines = [header, "-" * len(header)]
    for index, candidate in enumerate(candidates, start=1):
        email = candidate.email_if_public or "-"
        owner = candidate.owner_name[:32]
        lines.append(
            f"{index:>2}  {owner:<32} {candidate.owner_type:<12} "
            f"{candidate.repos_count:>5} {candidate.genesys_repos_count:>4} "
            f"{candidate.stars:>6}  {email[:34]:<34} {(candidate.top_repo or '-')[:width * 12]}"
        )
    return "\n".join(lines)


def split_selection(raw: str, count: int) -> List[int]:
    """Parse ``--add`` input: ``1,3-5,all`` -> zero-based indexes."""
    selection: List[int] = []
    text = (raw or "").strip().lower()
    if not text:
        return selection
    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        if chunk in {"all", "*"}:
            return list(range(count))
        if "-" in chunk:
            start_raw, _, end_raw = chunk.partition("-")
            try:
                start, end = int(start_raw), int(end_raw)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            selection.extend(range(start - 1, end))
        else:
            try:
                selection.append(int(chunk) - 1)
            except ValueError:
                continue
    return sorted({index for index in selection if 0 <= index < count})