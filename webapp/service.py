"""Service layer bridging the web UI to the existing bot modules.

The UI never re-implements pipeline logic. Discovery, message generation,
rate limiting and delivery all run through the same modules the CLI uses, so
the two front ends cannot drift apart. This module is the thin seam that wraps
those modules with friendly dictionaries and live log forwarding.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from config_loader import Config, ConfigError, resolve_secret
from logging_setup import get_logger
from models import (
    CHANNEL_EMAIL,
    CHANNELS,
    DiscoveredSponsor,
    Sponsor,
    is_valid_email,
    now_iso,
)
from tracker import SponsorTracker, TrackerError

LOG = get_logger("webapp")

#: Serializes read-modify-write cycles on a profile's tracker file so a
#: discovery job and a delivery job cannot clobber each other's state.
WRITE_LOCK = threading.RLock()

#: Loggers whose records are interesting enough to stream into the UI.
_APP_LOGGERS = {
    "cli",
    "discovery",
    "scheduler",
    "prompt-engine",
    "tracker",
    "senders",
    "email-sender",
    "forum-sender",
    "llm",
    "webapp",
    # Content syndication pipeline.
    "promoter",
    "content-engine",
    "content-store",
    "publishers",
    "publisher-devto",
    "publisher-hashnode",
    "publisher-medium",
    "publisher-wp",
    "publisher-manual",
    "publisher-webhook",
    "state",
}


class _JobLogHandler(logging.Handler):
    """Forwards application log records into a background job's log."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        super().__init__(logging.INFO)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        if record.name not in _APP_LOGGERS and "." not in record.name:
            return
        try:
            self._sink(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never break the job
            pass


@contextlib.contextmanager
def capture_logs(sink: Callable[[str], None]):
    """Mirror application logs to ``sink`` for the duration of the block."""
    handler = _JobLogHandler(sink)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)


# --------------------------------------------------------------------------- #
# Wiring helpers
# --------------------------------------------------------------------------- #


def build_tracker(config: Config) -> SponsorTracker:
    return SponsorTracker(
        config.path("paths.tracker_file", "sponsors.json"),
        seed_sponsors=config.records("sponsors"),
        rate_limits=config.rate_limits,
    )


def build_renderer(config: Config, use_llm: bool):
    """Return an LLM renderer, or None. Raises when misconfigured."""
    if not use_llm:
        return None
    from llm import LLMError, SubprocessLLM

    command = config.list("scheduler.llm_command")
    if not command:
        raise ConfigError(
            "LLM rendering requested but scheduler.llm_command is empty. "
            'Set it to a local CLI, e.g. ["ollama", "run", "llama3.1"].'
        )
    try:
        renderer = SubprocessLLM(
            command,
            timeout=config.int("scheduler.llm_timeout_seconds", 120, minimum=5, maximum=900),
        )
    except LLMError as exc:
        raise ConfigError(str(exc)) from exc
    if not renderer.exists():
        raise ConfigError(f"renderer {renderer.describe()!r} is not on PATH")
    return renderer


def _context_for(tracker: SponsorTracker, sponsor: Sponsor) -> Dict[str, Any]:
    entry = tracker.seen_entry(sponsor.github or sponsor.name) or {}
    context = {
        "github_url": f"https://github.com/{sponsor.github}" if sponsor.github else "",
        "owner_type": sponsor.owner_type,
        "stars": sponsor.stars,
        "top_repo": entry.get("top_repo", ""),
        "genesys_repos_count": entry.get("genesys_repos_count", 0),
        "repos_count": entry.get("repos_count", 0),
        "notes": sponsor.notes,
    }
    return {key: value for key, value in context.items() if value not in ("", None, 0)}


# --------------------------------------------------------------------------- #
# GitHub connection
# --------------------------------------------------------------------------- #


def connect_github(
    *,
    token: str = "",
    token_env: str = "GITHUB_TOKEN",
    api_base: str = "https://api.github.com",
    user_agent: str = "OutreachWizard/1.0",
) -> Dict[str, Any]:
    """Validate a token and, if present, put it in the process environment.

    The token is never written to disk. Setting ``os.environ`` means every
    existing module (discovery, senders, scheduler) picks it up through the same
    ``*_env`` indirection the CLI uses.
    """
    from discovery import DiscoveryError, GitHubClient

    token = (token or "").strip()
    token_env = (token_env or "GITHUB_TOKEN").strip() or "GITHUB_TOKEN"
    client = GitHubClient(token=token, api_base=api_base, user_agent=user_agent)

    try:
        rate = client.rate_limit
    except DiscoveryError as exc:
        raise ValueError(f"could not reach GitHub: {exc}") from exc

    if not token:
        return {
            "connected": False,
            "mode": "anonymous",
            "login": "",
            "name": "",
            "token_env": token_env,
            "rate_limit": rate,
            "message": "No token provided - using the anonymous GitHub quota (10 searches/min).",
        }

    try:
        user = client.get_authenticated_user()
    except DiscoveryError as exc:
        raise ValueError(str(exc)) from exc

    os.environ[token_env] = token
    LOG.info("GitHub connected as %s", user.get("login"))
    return {
        "connected": True,
        "mode": "token",
        "login": str(user.get("login") or ""),
        "name": str(user.get("name") or ""),
        "avatar_url": str(user.get("avatar_url") or ""),
        "token_env": token_env,
        "rate_limit": rate,
        "message": f"Connected as {user.get('login') or 'GitHub user'}.",
    }


def disconnect_github(token_env: str = "GITHUB_TOKEN") -> Dict[str, Any]:
    os.environ.pop((token_env or "GITHUB_TOKEN").strip() or "GITHUB_TOKEN", None)
    return {"connected": False, "mode": "anonymous", "token_env": token_env}


def github_status(config: Optional[Config] = None) -> Dict[str, Any]:
    token_env = "GITHUB_TOKEN"
    api_base = "https://api.github.com"
    if config is not None:
        token_env = config.str("github.token_env", "GITHUB_TOKEN")
        api_base = config.str("github.api_base", "https://api.github.com")
    token = resolve_secret(token_env)
    return {
        "connected": bool(token),
        "token_env": token_env,
        "api_base": api_base,
        "token_set": bool(token),
    }


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def _write_discovery_cache(path: Path, candidates: List[DiscoveredSponsor]) -> None:
    import json

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": now_iso(),
            "count": len(candidates),
            "candidates": [candidate.to_dict() for candidate in candidates],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        LOG.warning("Could not write discovery cache %s: %s", path, exc)


def load_cached_candidates(config: Config) -> Dict[str, Any]:
    import json

    path = config.path("paths.discovery_cache", "discovered.json")
    if not path.is_file():
        return {"generated_at": "", "count": 0, "candidates": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {"generated_at": "", "count": 0, "candidates": []}
    candidates = payload.get("candidates")
    return {
        "generated_at": payload.get("generated_at", ""),
        "count": int(payload.get("count") or 0),
        "candidates": candidates if isinstance(candidates, list) else [],
    }


def run_discovery(
    config: Config,
    ctx,
    *,
    topics: Optional[Iterable[str]] = None,
    min_org_repos: Optional[int] = None,
    min_individual_genesys_repos: Optional[int] = None,
    min_stars: Optional[int] = None,
    exclude_forks: Optional[bool] = None,
    per_page: Optional[int] = None,
    max_pages: Optional[int] = None,
    max_owner_lookups: Optional[int] = None,
    scrape_public_emails: Optional[bool] = None,
    email_scrape_max_sites: Optional[int] = None,
) -> Dict[str, Any]:
    """Run a full discovery sweep and fold the result into this profile."""
    from discovery import (
        DiscoveryError,
        client_from_config,
        discover_sponsors,
    )

    topic_list = [str(topic).strip().lstrip("#") for topic in (topics or config.github_topics) if str(topic).strip()]
    if not topic_list:
        raise ValueError("no topics configured - add at least one search topic")

    client = client_from_config(config)
    with capture_logs(ctx.log):
        try:
            rate = client.rate_limit
            ctx.progress(topics=topic_list, stage="searching")
            ctx.log(
                f"GitHub search quota: {rate.get('remaining', '?')}/{rate.get('limit', '?')} "
                f"(resets {rate.get('reset', '?')})"
            )
        except DiscoveryError as exc:
            ctx.log(f"Could not read rate limit: {exc}")

        candidates = discover_sponsors(
            client,
            topic_list,
            min_org_repos=int(min_org_repos if min_org_repos is not None else config.int("github.filters.min_org_repos", 5, minimum=1)),
            min_individual_genesys_repos=int(
                min_individual_genesys_repos
                if min_individual_genesys_repos is not None
                else config.int("github.filters.min_individual_genesys_repos", 3, minimum=1)
            ),
            min_stars=int(min_stars if min_stars is not None else config.int("github.filters.min_stars", 0)),
            exclude_forks=bool(exclude_forks if exclude_forks is not None else config.bool("github.filters.exclude_forks", True)),
            per_page=int(per_page if per_page is not None else config.int("github.per_page", 100, minimum=1, maximum=100)),
            max_pages=int(max_pages if max_pages is not None else config.int("github.max_pages", 3, minimum=1, maximum=10)),
            max_owner_lookups=int(max_owner_lookups if max_owner_lookups is not None else config.int("github.filters.max_owner_lookups", 40, minimum=1)),
            scrape_public_emails=bool(
                scrape_public_emails
                if scrape_public_emails is not None
                else config.bool("github.scrape_public_emails", True)
            ),
            email_scrape_max_sites=int(
                email_scrape_max_sites
                if email_scrape_max_sites is not None
                else config.int("github.email_scrape_max_sites", 3, minimum=0, maximum=10)
            ),
        )

    ctx.check_cancelled()

    with WRITE_LOCK:
        tracker = build_tracker(config)
        fresh: List[DiscoveredSponsor] = []
        already: List[DiscoveredSponsor] = []
        for candidate in candidates:
            (already if tracker.get(candidate.owner_name) else fresh).append(candidate)
            tracker.mark_seen(candidate)
        _write_discovery_cache(config.path("paths.discovery_cache", "discovered.json"), candidates)
        tracker.save()

    ctx.progress(stage="done", qualified=len(candidates), fresh=len(fresh))
    ctx.log(f"Discovery complete: {len(candidates)} owner(s) qualified, {len(fresh)} new.")
    return {
        "topics": topic_list,
        "qualified": len(candidates),
        "new": len(fresh),
        "already_tracked": [candidate.owner_name for candidate in already],
        "candidates": [candidate.to_dict() for candidate in candidates],
    }


# --------------------------------------------------------------------------- #
# Approval
# --------------------------------------------------------------------------- #


def approve_candidates(
    config: Config, raw_candidates: List[Dict[str, Any]], *, channel: str = CHANNEL_EMAIL
) -> Dict[str, Any]:
    """Add approved discovery hits to the tracker as ``new`` sponsors."""
    channel = (channel or CHANNEL_EMAIL).strip().lower()
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}; expected one of {', '.join(CHANNELS)}")
    if not raw_candidates:
        raise ValueError("no candidates selected")

    created = 0
    updated = 0
    errors: List[str] = []
    added: List[Dict[str, Any]] = []

    with WRITE_LOCK:
        tracker = build_tracker(config)
        for raw in raw_candidates:
            try:
                discovered = DiscoveredSponsor.from_dict(raw)
            except (ValueError, TypeError) as exc:
                errors.append(f"{raw.get('owner_name', '?')}: {exc}")
                continue
            if not discovered.owner_name:
                errors.append("candidate has no owner_name")
                continue
            sponsor = discovered.to_sponsor(channel=channel)
            if channel == CHANNEL_EMAIL and not is_valid_email(sponsor.email):
                sponsor.notes = (
                    sponsor.notes + " | no public email found - add one manually"
                ).strip()
            try:
                stored, was_new = tracker.add(sponsor)
            except TrackerError as exc:
                errors.append(f"{discovered.owner_name}: {exc}")
                continue
            tracker.mark_seen(discovered)
            created += int(was_new)
            updated += int(not was_new)
            added.append(stored.to_dict())

        tracker.save()
    return {
        "created": created,
        "updated": updated,
        "errors": errors,
        "sponsors": added,
        "channel": channel,
    }


def add_sponsor(config: Config, data: Dict[str, Any]) -> Dict[str, Any]:
    """Add or enrich a single sponsor from a manual entry."""
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    channel = str(data.get("channel") or CHANNEL_EMAIL).strip().lower()
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
    sponsor = Sponsor(
        name=name,
        email=str(data.get("email") or "").strip().lower(),
        channel=channel,
        contact=str(data.get("contact") or ""),
        company=str(data.get("company") or ""),
        github=str(data.get("github") or "").strip().lstrip("@"),
        website=str(data.get("website") or ""),
        notes=str(data.get("notes") or ""),
        source="manual",
    )
    require_email = channel == CHANNEL_EMAIL and not data.get("allow_no_email")
    errors = sponsor.validate(require_email=require_email)
    if errors:
        raise ValueError("; ".join(errors))
    with WRITE_LOCK:
        tracker = build_tracker(config)
        stored, created = tracker.add(sponsor)
        tracker.save()
    return {"created": created, "sponsor": stored.to_dict()}


def mark_sponsor(config: Config, name: str, status: str) -> Dict[str, Any]:
    with WRITE_LOCK:
        tracker = build_tracker(config)
        sponsor = tracker.update_status(name, status)
        tracker.save()
    return {"sponsor": sponsor.to_dict()}


def remove_sponsor(config: Config, name: str) -> Dict[str, Any]:
    with WRITE_LOCK:
        tracker = build_tracker(config)
        sponsor = tracker.remove(name)
        tracker.save()
    return {"removed": sponsor.name}


# --------------------------------------------------------------------------- #
# Preview and send
# --------------------------------------------------------------------------- #


def generate_preview(
    config: Config,
    sponsor_name: str,
    *,
    channel: Optional[str] = None,
    use_llm: bool = False,
) -> Dict[str, Any]:
    from prompt_engine import (
        generate,
        plugin_data_from_config,
        render_prompt,
        word_count_message,
        word_limit,
    )

    tracker = build_tracker(config)
    if not sponsor_name:
        queue = tracker.next_batch(1)
        if not queue:
            raise ValueError("no sponsor in the 'new' queue to preview")
        sponsor = queue[0]
    else:
        sponsor = tracker.get(sponsor_name)
        if sponsor is None:
            raise ValueError(f"sponsor {sponsor_name!r} is not in the tracker")

    target_channel = (channel or sponsor.channel or CHANNEL_EMAIL).lower()
    renderer = build_renderer(config, use_llm)
    plugin = plugin_data_from_config(config)
    context = _context_for(tracker, sponsor)

    message = generate(
        plugin,
        target_channel,
        sponsor.name,
        llm_renderer=renderer,
        sponsor_context=context,
    )
    words = word_count_message(target_channel, message)
    limit = word_limit(target_channel)
    return {
        "sponsor": sponsor.name,
        "channel": target_channel,
        "message": message,
        "words": words,
        "word_limit": limit,
        "prompt": render_prompt(plugin, target_channel, sponsor.name, sponsor_context=context)
        if use_llm
        else "",
    }


def send_sponsors(
    config: Config,
    ctx,
    *,
    names: Optional[List[str]] = None,
    dry_run: bool = True,
    use_llm: bool = False,
    batch: Optional[int] = None,
) -> Dict[str, Any]:
    """Deliver to an explicit (approved) list, or the next batch if none given."""
    from scheduler import Scheduler

    renderer = build_renderer(config, use_llm)

    WRITE_LOCK.acquire()
    try:
        tracker = build_tracker(config)
        scheduler = Scheduler(
            config,
            tracker,
            dry_run=dry_run,
            batch_size=int(batch) if batch else None,
            llm_renderer=renderer,
        )

        if names:
            sponsors: List[Sponsor] = []
            missing: List[str] = []
            for name in names:
                sponsor = tracker.get(name)
                if sponsor is None:
                    missing.append(name)
                else:
                    sponsors.append(sponsor)
            if missing:
                ctx.log(f"WARNING not in tracker, skipped: {', '.join(missing)}")
        else:
            sponsors = tracker.next_batch(scheduler.batch_size)

        if not sponsors:
            ctx.log("No sponsors to deliver.")
            return {
                "result": {
                    "attempted": 0,
                    "sent": 0,
                    "failed": 0,
                    "skipped": 0,
                    "deliveries": [],
                    "notes": ["nothing to send"],
                },
                "summary": tracker.summary(),
            }

        ctx.progress(stage="sending", total=len(sponsors))
        ctx.log(
            f"{'DRY RUN' if dry_run else 'LIVE'} delivery to {len(sponsors)} sponsor(s) "
            f"(batch={scheduler.batch_size})"
        )
        with capture_logs(ctx.log):
            result = scheduler.deliver(sponsors)
        tracker.save()
        ctx.progress(stage="done", sent=result.sent, failed=result.failed)
        summary = tracker.summary()
    finally:
        WRITE_LOCK.release()

    return {"result": result.to_dict(), "summary": summary}


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Content syndication
#
# Same rule as the rest of this module: the wizard calls the CLI's modules, it
# never re-implements them. Every long operation runs through `Promoter`, so the
# web UI inherits the quality gate, the per-platform quotas, the cooldown and the
# persist-after-every-publish guarantee for free.
# --------------------------------------------------------------------------- #


def build_content_store(config: Config):
    """The campaign's content ledger."""
    from promoter import build_store

    return build_store(config)


def content_summary(config: Config) -> Dict[str, Any]:
    """Ledger summary plus the registry, for the Content step."""
    from content_engine import status_help
    from platforms import catalog
    from publishers import check_all

    store = build_content_store(config)
    data = content_data_from_config(config)
    prepared: List[Dict[str, Any]] = []
    for item in store.all():
        entry = item.to_dict()
        entry["gate"] = _gate_for(data, item)
        prepared.append(entry)
    return {
        "enabled": config.publishing_enabled,
        "content": data,
        "items": prepared,
        "summary": store.summary(),
        "statuses": list(config.content_statuses),
        "status_help": status_help(),
        "platforms": catalog(),
        "readiness": check_all(config, dry_run=True),
        "enforce_gate": config.bool("publishing.enforce_quality_gate", True),
        "require_approval": config.bool("publishing.require_approval", True),
        "default_platforms": content_data_from_config(config).get("default_platforms") or [],
    }


def content_data_from_config(config: Config) -> Dict[str, Any]:
    from content_engine import content_data_from_config as _project

    return _project(config)


def _gate_for(data: Dict[str, Any], item) -> List[Dict[str, Any]]:  # noqa: ANN001
    """Per-platform gate findings for one article, as structured rows."""
    from content_engine import prepare_for_platform

    rows: List[Dict[str, Any]] = []
    for platform in item.platforms:
        shaped = prepare_for_platform(
            data,
            title=item.title,
            body=item.body_markdown,
            summary=item.summary,
            tags=item.tags,
            platform=platform,
        )
        rows.append(
            {
                "platform": platform,
                "publishable": shaped["publishable"],
                "words": shaped["words"],
                "tags": shaped["tags"],
                "problems": shaped["problems"],
            }
        )
    return rows


def draft_article(
    config: Config,
    *,
    title: str = "",
    topic: str = "",
    platforms: Optional[List[str]] = None,
    use_llm: bool = False,
    approve: bool = False,
) -> Dict[str, Any]:
    """Compose an article into the ledger. Never transmits."""
    from content_store import ContentStore
    from content_engine import generate
    from models import ContentItem, PLATFORM_IDS

    data = content_data_from_config(config)
    store = build_content_store(config)
    targets = [pid for pid in (platforms or config.platforms_enabled()) if pid in PLATFORM_IDS]
    renderer = build_renderer(config, use_llm)
    composed = generate(
        data,
        title=title,
        topic=topic,
        platform=targets[0] if targets else "",
        llm_renderer=renderer,
    )
    item = ContentItem(
        id=ContentStore.make_id(composed["title"], topic),
        title=composed["title"],
        body_markdown=composed["body"],
        summary=composed["summary"],
        topic=topic,
        platforms=targets,
        tags=[str(tag) for tag in (data.get("tags") or [])],
        canonical_url=str(data.get("canonical_base_url") or ""),
        status="draft",
        source="composed",
        words=int(composed.get("words") or 0),
    )
    item.fingerprint = item.compute_fingerprint()
    with WRITE_LOCK:
        stored, created = store.add(item)
        if approve and stored.status == "draft":
            stored = store.approve(stored.id)
        store.save()
    return {
        "created": created,
        "item": stored.to_dict(),
        "gate": _gate_for(data, stored),
        "renderer": composed.get("renderer") or "deterministic",
    }


def show_article(config: Config, item_id: str) -> Dict[str, Any]:
    store = build_content_store(config)
    item = store.require(item_id)
    return {"item": item.to_dict(), "gate": _gate_for(content_data_from_config(config), item)}


def update_article(config: Config, item_id: str, body: str, *, title: str = "") -> Dict[str, Any]:
    """Replace an article's text from the editor, keeping its ledger intact."""
    text = (body or "").strip()
    if not text:
        raise ValueError("the article body cannot be empty")
    store = build_content_store(config)
    with WRITE_LOCK:
        item = store.require(item_id)
        item.body_markdown = text
        if title:
            item.title = title.strip()
        item.words = len(text.split())
        item.fingerprint = item.compute_fingerprint()
        item.updated_at = now_iso()
        item.last_error = ""
        item.sync_status()
        store.save()
    return {"item": item.to_dict(), "gate": _gate_for(content_data_from_config(config), item)}


def mark_article(config: Config, item_id: str, status: str) -> Dict[str, Any]:
    store = build_content_store(config)
    with WRITE_LOCK:
        item = store.update_status(item_id, status)
        store.save()
    return {"item": item.to_dict()}


def approve_article(config: Config, item_id: str) -> Dict[str, Any]:
    store = build_content_store(config)
    with WRITE_LOCK:
        item = store.approve(item_id)
        store.save()
    return {
        "item": item.to_dict(),
        "pending_platforms": item.pending_platforms(),
        "publishing_enabled": config.publishing_enabled,
    }


def confirm_article(config: Config, item_id: str, platform: str, url: str) -> Dict[str, Any]:
    store = build_content_store(config)
    with WRITE_LOCK:
        item = store.confirm(item_id, platform, url=url)
        store.save()
    return {"item": item.to_dict(), "live_urls": item.live_urls()}


def reset_article(config: Config, item_id: str, platform: str) -> Dict[str, Any]:
    store = build_content_store(config)
    with WRITE_LOCK:
        item = store.unconfirm(item_id, platform)
        store.save()
    return {"item": item.to_dict()}


def remove_article(config: Config, item_id: str) -> Dict[str, Any]:
    store = build_content_store(config)
    with WRITE_LOCK:
        item = store.remove(item_id)
        store.save()
    return {"removed": item.id}


def publish_content(
    config: Config,
    ctx,
    *,
    item_ids: Optional[List[str]] = None,
    platforms: Optional[List[str]] = None,
    dry_run: bool = True,
    batch: Optional[int] = None,
) -> Dict[str, Any]:
    """Run one publish tick on a background job.

    Delivery goes through `Promoter.tick()`, so the web UI cannot publish an
    article the CLI would refuse, and every successful publish is persisted
    before the next one is attempted.
    """
    from promoter import Promoter

    if not config.publishing_enabled:
        raise ValueError(
            "content syndication is off - set content.enabled: true and at least one "
            "platforms.<id>.enabled: true"
        )

    WRITE_LOCK.acquire()
    try:
        store = build_content_store(config)
        promoter = Promoter(
            config,
            store,
            dry_run=dry_run,
            batch_size=int(batch) if batch else None,
        )
        pending = store.next_targets(
            promoter.batch_size,
            platforms=platforms or promoter.platforms,
            item_ids=item_ids,
            statuses=promoter.publishable_statuses,
        )
        if not pending:
            ctx.log("Nothing to publish. Draft an article and approve it first.")
            return {"result": _empty_tick(), "summary": store.summary()}

        ctx.progress(stage="publishing", total=len(pending))
        ctx.log(
            f"{'DRY RUN' if dry_run else 'LIVE'} publish of {len(pending)} "
            f"article/platform pair(s)"
        )
        for target in pending:
            ctx.log(f"  {target.item_id} -> {target.platform}: {target.item.title}")
        with capture_logs(ctx.log):
            result = promoter.publish_targets(pending)
        store.save()
        ctx.progress(
            stage="done",
            published=result.published,
            drafted=result.drafted,
            queued=result.queued,
            failed=result.failed,
        )
        summary = store.summary()
    finally:
        WRITE_LOCK.release()

    payload: Dict[str, Any] = {"result": result.to_dict(), "summary": summary}
    if result.queued:
        from content_engine import approve_hint

        payload["hint"] = approve_hint()
        ctx.log("Manual platforms need a confirmed URL. " + approve_hint())
    return payload


def _empty_tick() -> Dict[str, Any]:
    return {
        "attempted": 0,
        "published": 0,
        "drafted": 0,
        "queued": 0,
        "failed": 0,
        "skipped": 0,
        "records": [],
        "notes": ["nothing to publish"],
    }


def set_content_secrets(
    config: Config, *, values: Dict[str, str]
) -> Dict[str, Any]:
    """Put platform credentials in the process environment only - never on disk.

    `values` maps a platform id to the credential the operator typed. Which
    environment variable receives it comes from that platform's own config, so
    the UI never has to know the variable names.
    """
    from publishers import get_publisher

    applied: List[str] = []
    problems: Dict[str, str] = {}
    for platform_id, value in (values or {}).items():
        secret = str(value or "").strip()
        if not secret:
            continue
        try:
            publisher = get_publisher(platform_id, config, dry_run=True)
        except Exception as exc:  # noqa: BLE001 - one bad target must not block the rest
            problems[platform_id] = f"{type(exc).__name__}: {exc}"
            continue
        env_names = getattr(publisher, "token_envs", [])
        if not env_names:
            problems[platform_id] = f"{platform_id} needs no credential"
            continue
        os.environ[env_names[0]] = secret
        applied.append(f"{platform_id} -> {env_names[0]}")

    LOG.info("Publishing credentials updated in memory: %s", ", ".join(applied) or "(none)")
    return {"applied": applied, "problems": problems}


def status(config: Config, tracker: SponsorTracker) -> Dict[str, Any]:
    return {"config": config.describe(), "pipeline": tracker.summary()}


def set_secrets(
    config: Config,
    *,
    smtp_password: str = "",
    forum_api_key: str = "",
    github_token: str = "",
) -> Dict[str, Any]:
    """Place secrets in the environment for this process only - never on disk."""
    applied: List[str] = []
    if smtp_password:
        os.environ[config.str("email.password_env", "SMTP_PASSWORD") or "SMTP_PASSWORD"] = smtp_password
        applied.append("SMTP_PASSWORD")
    if forum_api_key:
        os.environ[config.str("forum.api_key_env", "FORUM_API_KEY") or "FORUM_API_KEY"] = forum_api_key
        applied.append("FORUM_API_KEY")
    if github_token:
        os.environ[config.str("github.token_env", "GITHUB_TOKEN") or "GITHUB_TOKEN"] = github_token
        applied.append("GITHUB_TOKEN")
    LOG.info("Secrets updated in memory: %s", ", ".join(applied) or "(none)")
    return {
        "applied": applied,
        "smtp_password_set": bool(resolve_secret(config.str("email.password_env", "SMTP_PASSWORD"))),
        "forum_api_key_set": bool(resolve_secret(config.str("forum.api_key_env", "FORUM_API_KEY"))),
        "github_token_set": bool(resolve_secret(config.str("github.token_env", "GITHUB_TOKEN"))),
    }
