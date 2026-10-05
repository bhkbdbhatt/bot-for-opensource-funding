#!/usr/bin/env python3
"""GenesysPluginSponsorBot - command line interface.

Two pipelines, one binary.

Outreach (1:1, a named person):
    python main.py discover        # scrape GitHub topics for new prospects
    python main.py add-sponsor ... # add a prospect manually
    python main.py generate ...    # dry-run: print the message, send nothing
    python main.py run             # start the rate-limited scheduler
    python main.py status          # pipeline summary
    python main.py list            # every sponsor and status

Content syndication (1:N, a platform audience):
    python main.py platforms       # what is configured and whether it is ready
    python main.py draft           # compose an article into content.json
    python main.py content ...     # review / approve / confirm the ledger
    python main.py publish         # send approved articles to their platforms

Web UI and accounts:
    python main.py auth ...        # manage web UI accounts and 2FA enrolment
    python main.py ui              # launch the local web wizard
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from config_loader import Config, ConfigError
from content_store import ContentStoreError
from logging_setup import get_logger, setup_logging
from models import (
    CHANNELS,
    CONTENT_STATUSES,
    PLATFORM_IDS,
    STATUSES,
    ContentItem,
    Sponsor,
    is_valid_email,
    now_iso,
    slugify,
)
from tracker import SponsorNotFound, SponsorTracker, TrackerError
from webapp.auth import (
    MASTER_ENV,
    USERS_FILENAME,
    AuthError,
    UserStore,
    describe_store,
    generate_recovery_codes,
    normalize_email,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

BANNER = r"""
  ___      _  _  ___       _    _             _
 / __|__ _| || |/ _ \ _ _ (_)__| |__ _ _ _  _| |__ _ _  _ __ _ _ _  _
 \__ \/ _` || | (_) | '_ \| |  | / _` | '_ \| |  _` | '_ \| '_ \ || |
 |___/\__,_||_|\___/| .__/|_|  |_\__,_|_||_||_|\__,_| .__/| .__/\_, |
                   |_|           |__|              |_|   |_|
"""


# --------------------------------------------------------------------------- #
# Console helpers
# --------------------------------------------------------------------------- #


def out(text: str = "") -> None:
    print(text)


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def rule(title: str = "") -> None:
    width = 78
    out(f"-- {title} ".ljust(width, "-") if title else "-" * width)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def load_config(args: argparse.Namespace) -> Config:
    config = Config.load(args.config)
    setup_logging(
        log_file=config.path("paths.log_file", "bot.log"),
        level=args.log_level or config.str("paths.log_level", "INFO"),
        quiet=args.quiet,
        max_bytes=config.int("paths.log_max_bytes", 2 * 1024 * 1024, minimum=10_000),
        backup_count=config.int("paths.log_backup_count", 3, minimum=1),
    )
    for warning in config.warnings:
        get_logger("cli").warning("%s", warning)
    return config


def build_tracker(config: Config, args: argparse.Namespace) -> SponsorTracker:
    path = Path(args.tracker).expanduser() if args.tracker else config.path("paths.tracker_file", "sponsors.json")
    return SponsorTracker(
        path,
        seed_sponsors=config.records("sponsors"),
        rate_limits=config.rate_limits,
    )


def build_scheduler(config: Config, tracker: SponsorTracker, args: argparse.Namespace):
    from llm import LLMError, SubprocessLLM
    from scheduler import Scheduler

    renderer = None
    if getattr(args, "llm", False):
        command = config.list("scheduler.llm_command")
        if not command:
            raise ConfigError(
                "--llm requested but scheduler.llm_command is empty. "
                'Set it to a local CLI, e.g. ["ollama", "run", "llama3.1"].'
            )
        try:
            renderer = SubprocessLLM(
                command,
                timeout=config.int("scheduler.llm_timeout_seconds", 120, minimum=5, maximum=900),
            )
        except LLMError as exc:
            raise ConfigError(f"--llm requested but {exc}") from exc
        if not renderer.exists():
            raise ConfigError(f"--llm requested but {renderer.describe()!r} is not on PATH")
        LOG_RENDERER = get_logger("cli")
        LOG_RENDERER.info("Using external renderer: %s", renderer.describe())
    return Scheduler(
        config,
        tracker,
        dry_run=True if getattr(args, "dry_run", False) else None,
        batch_size=getattr(args, "batch", None),
        llm_renderer=renderer,
    )


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_discover(args: argparse.Namespace) -> int:
    from discovery import (
        DiscoveryError,
        client_from_config,
        discover_sponsors,
        render_table,
    )

    config = load_config(args)
    log = get_logger("cli")
    client = client_from_config(config)

    try:
        limit = client.rate_limit
        log.info(
            "GitHub search quota: %s/%s (resets %s)",
            limit.get("remaining", "?"),
            limit.get("limit", "?"),
            limit.get("reset", "?"),
        )
    except DiscoveryError as exc:
        log.warning("Could not read rate limit: %s", exc)

    topics = args.topics.split(",") if args.topics else config.github_topics
    topics = [topic.strip().lstrip("#") for topic in topics if topic.strip()]
    if not topics:
        log.error("No topics to scan")
        return EXIT_USAGE

    log.info("Scanning topics: %s", ", ".join(topics))
    candidates = discover_sponsors(
        client,
        topics,
        min_org_repos=config.int("github.filters.min_org_repos", 5, minimum=1),
        min_individual_genesys_repos=config.int("github.filters.min_individual_genesys_repos", 3, minimum=1),
        min_stars=config.int("github.filters.min_stars", 0),
        exclude_forks=config.bool("github.filters.exclude_forks", True),
        per_page=config.int("github.per_page", 100, minimum=1, maximum=100),
        max_pages=config.int("github.max_pages", 3, minimum=1, maximum=10),
        max_owner_lookups=config.int("github.filters.max_owner_lookups", 40, minimum=1),
        scrape_public_emails=not args.no_emails and config.bool("github.scrape_public_emails", True),
        email_scrape_max_sites=config.int("github.email_scrape_max_sites", 3, minimum=0, maximum=10),
    )

    tracker = build_tracker(config, args)
    fresh: List[Any] = []
    already: List[Any] = []
    for candidate in candidates:
        existing = tracker.get(candidate.owner_name)
        (already if existing else fresh).append(candidate)
        tracker.mark_seen(candidate)

    cache_path = config.path("paths.discovery_cache", "discovered.json")
    _write_discovery_cache(cache_path, candidates, tracker)
    tracker.save()

    if args.json:
        emit_json(
            {
                "topics": topics,
                "qualified": len(candidates),
                "new": [candidate.to_dict() for candidate in fresh],
                "already_tracked": [candidate.owner_name for candidate in already],
                "cache": str(cache_path),
            }
        )
        return EXIT_OK

    rule("Discovery results")
    out(f"Topics scanned : {', '.join(topics)}")
    out(f"Owners matched : {len(candidates)}  (new: {len(fresh)}, already tracked: {len(already)})")
    out(f"Cache written  : {cache_path}")
    out()
    out(render_table(fresh or candidates))
    out()

    if not candidates:
        log.info("No candidates matched the filter. Try relaxing github.filters in config.yaml")
        return EXIT_OK

    if already:
        out("Already tracked: " + ", ".join(candidate.owner_name for candidate in already))
        out()

    pool = fresh or candidates
    indexes = _choose_candidates(args, pool, log)
    if not indexes:
        log.info("No candidates selected. Nothing added to the tracker.")
        return EXIT_OK

    channel = args.channel
    added = 0
    for index in indexes:
        candidate = pool[index]
        sponsor = candidate.to_sponsor(channel=channel)
        if args.channel == "email" and not is_valid_email(sponsor.email):
            sponsor.notes = (sponsor.notes + " | no public email found - add one manually").strip()
        try:
            _, created = tracker.add(sponsor)
            added += int(created)
        except TrackerError as exc:
            log.error("Could not add %s: %s", candidate.owner_name, exc)
    tracker.save()
    out()
    rule("Done")
    out(f"Added {added} new sponsor(s) as status 'new' on the '{channel}' channel.")
    out("Review with: python main.py list")
    return EXIT_OK


def _choose_candidates(args: argparse.Namespace, pool: Sequence[Any], log) -> List[int]:
    from discovery import split_selection

    count = len(pool)
    if args.limit:
        count = min(count, args.limit)
    if args.add:
        return split_selection(args.add, count)
    if args.yes:
        return list(range(count))
    if args.no_input or not sys.stdin.isatty():
        log.info("Non-interactive session: use --add 1,2,3 or --yes to select candidates")
        return []
    try:
        answer = input(f"Add which candidates? [1-{count}, ranges, 'all', 'none'] (default none): ")
    except (EOFError, KeyboardInterrupt):
        out()
        return []
    answer = (answer or "").strip().lower()
    if not answer or answer in {"none", "n", "no", "q", "quit"}:
        return []
    return split_selection(answer, count)


def _write_discovery_cache(path: Path, candidates: Sequence[Any], tracker: SponsorTracker) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": now_iso(),
            "count": len(candidates),
            "candidates": [candidate.to_dict() for candidate in candidates],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        get_logger("cli").warning("Could not write discovery cache %s: %s", path, exc)


def cmd_add_sponsor(args: argparse.Namespace) -> int:
    config = load_config(args)
    log = get_logger("cli")
    tracker = build_tracker(config, args)

    sponsor = Sponsor(
        name=args.name.strip(),
        email=(args.email or "").strip().lower(),
        channel=args.channel,
        status=args.status,
        contact=args.contact or "",
        company=args.company or "",
        github=(args.github or "").strip().lstrip("@"),
        website=args.website or "",
        notes=args.notes or "",
        source="manual",
    )
    errors = sponsor.validate(require_email=not args.no_email)
    if errors:
        for error in errors:
            log.error("Invalid sponsor: %s", error)
        return EXIT_USAGE

    try:
        stored, created = tracker.add(sponsor)
    except TrackerError as exc:
        log.error("Could not add sponsor: %s", exc)
        return EXIT_ERROR
    tracker.save()

    out(("Added " if created else "Updated ") + stored.summary_line())
    if not created:
        log.info("Existing pipeline state was preserved; only empty fields were filled in")
    if stored.channel == "email" and not stored.email:
        log.warning(
            "%s has no email address - the scheduler will skip it until you add one "
            "(python main.py mark --name %s ... does not set email; edit %s or re-add)",
            stored.name,
            stored.name,
            tracker.path,
        )
    return EXIT_OK


def cmd_generate(args: argparse.Namespace) -> int:
    config = load_config(args)
    log = get_logger("cli")
    tracker = build_tracker(config, args)

    if args.sponsor:
        sponsor = tracker.get(args.sponsor)
        if sponsor is None:
            matches = [item for item in tracker.all() if args.sponsor.lower() in item.name.lower()]
            if len(matches) == 1:
                sponsor = matches[0]
            elif len(matches) > 1:
                log.error("Ambiguous sponsor %r: %s", args.sponsor, ", ".join(item.name for item in matches))
                return EXIT_USAGE
            else:
                log.error("Sponsor %r is not in the tracker", args.sponsor)
                return EXIT_ERROR
    else:
        queue = tracker.next_batch(1)
        if not queue:
            log.error("No sponsors available. Run 'python main.py discover' or 'add-sponsor' first.")
            return EXIT_ERROR
        sponsor = queue[0]
        log.info("No --sponsor given; using next in queue: %s", sponsor.name)

    channel = args.channel or sponsor.channel
    if channel not in CHANNELS:
        log.error("Unknown channel %r", channel)
        return EXIT_USAGE

    renderer = None
    if args.llm:
        try:
            scheduler = build_scheduler(config, tracker, args)
            renderer = scheduler.llm_renderer
            if renderer is None:
                raise ConfigError("--llm requested but no renderer is configured")
        except ConfigError as exc:
            log.error("%s", exc)
            return EXIT_USAGE

    from prompt_engine import (
        generate,
        plugin_data_from_config,
        render_prompt,
        word_count_message,
        word_limit,
    )

    plugin = plugin_data_from_config(config)
    scheduler_view = _context_for(tracker, sponsor)
    message = generate(
        plugin, channel, sponsor.name, llm_renderer=renderer, sponsor_context=scheduler_view
    )
    words = word_count_message(channel, message)
    limit = word_limit(channel)

    if args.prompt:
        out(render_prompt(plugin, channel, sponsor.name, sponsor_context=scheduler_view))
        return EXIT_OK

    payload = {
        "sponsor": sponsor.name,
        "channel": channel,
        "words": words,
        "word_limit": limit,
        "message": message,
    }
    if args.json:
        emit_json(payload)
    else:
        rule(f"{sponsor.name} -> {channel} ({words}/{limit} words)")
        out(message)
        out()
        if words > limit:
            log.warning("Message is %d words, over the %d word budget", words, limit)
        log.info("Dry run only - nothing was sent.")

    if args.out:
        target = Path(args.out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(message, encoding="utf-8")
        log.info("Message written to %s", target)
    return EXIT_OK


def _context_for(tracker: SponsorTracker, sponsor: Sponsor) -> Dict[str, Any]:
    entry = tracker.seen_entry(sponsor.github or sponsor.name) or {}
    context = {
        "github_url": f"https://github.com/{sponsor.github}" if sponsor.github else "",
        "owner_type": sponsor.owner_type,
        "stars": sponsor.stars,
        "top_repo": entry.get("top_repo", ""),
        "genesys_repos_count": entry.get("genesys_repos_count", 0),
        "notes": sponsor.notes,
    }
    return {key: value for key, value in context.items() if value not in ("", None, 0)}


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args)
    log = get_logger("cli")
    if args.dry_run:
        log.warning("DRY RUN: messages will be generated and logged, never transmitted")

    tracker = build_tracker(config, args)
    try:
        scheduler = build_scheduler(config, tracker, args)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_USAGE

    out(BANNER)
    out(f"Plugin      : {config.str('plugin.name')}")
    out(f"Pipeline    : {tracker.path}")
    out(f"Mode        : {'DRY RUN' if scheduler.dry_run else 'LIVE'}")
    out(f"Rate limits : {config.rate_limits}")
    out(f"Usage today : {tracker.daily_usage()}")
    out()

    if args.once:
        result = scheduler.tick()
        tracker.save()
        out()
        rule("Tick result")
        out(f"attempted={result.attempted} sent={result.sent} failed={result.failed} skipped={result.skipped}")
        for record in result.deliveries:
            flag = "OK " if record.ok else "ERR"
            out(f"  [{flag}] {record.sponsor:<28} {record.channel:<6} {record.words:>3}w  {record.detail}")
        for note in result.notes:
            out(f"  note: {note}")
        out()
        out(json.dumps(result.to_dict(), indent=2, default=str)) if args.json else None
        return EXIT_OK if result.failed == 0 else EXIT_ERROR

    scheduler.run_forever(interval=args.interval, max_ticks=args.max_ticks)
    tracker.save()
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    from publishers import check_all
    from senders import get_sender

    config = load_config(args)
    tracker = build_tracker(config, args)
    summary = tracker.summary()
    described = config.describe()

    content: Dict[str, Any] = {}
    if config.publishing_enabled or config.bool("content.enabled", False):
        try:
            from promoter import build_store

            store = build_store(config)
            content = store.summary()
        except Exception as exc:  # noqa: BLE001 - status must never fail on this
            content = {"error": f"{type(exc).__name__}: {exc}"}

    if args.json:
        payload = {"config": described, "pipeline": summary}
        if content:
            payload["content"] = content
        emit_json(payload)
        return EXIT_OK

    rule("GenesysPluginSponsorBot")
    out(f"Plugin       : {described['plugin']}")
    out(f"Repository   : {described['repo_url'] or '-'}")
    out(f"Config       : {described['config_file']}")
    out(f"Tracker      : {summary['tracker_file']}")
    out(f"GitHub token : {'set' if described['github_token_set'] else 'not set'}")
    out(f"Email        : {'enabled' if described['email_enabled'] else 'disabled'} "
        f"({described['smtp']}, password {'set' if described['smtp_password_set'] else 'MISSING'})")
    out(f"Forum        : {'enabled' if described['forum_enabled'] else 'disabled'} "
        f"({described['forum_mode']} mode)")
    out(f"Content      : {'enabled' if described['content_enabled'] else 'disabled'} "
        f"(targets: {', '.join(described['content_platforms']) or 'none'})")
    out()

    rule("Pipeline")
    total = summary["total"]
    for status in STATUSES:
        if status == "failed":
            count = summary["failures"]
        else:
            count = summary["by_status"].get(status, 0)
        share = (count / total * 100) if total else 0
        bar = "#" * int(share / 2)
        out(f"  {status:<10} {count:>4}  {bar}")
    out(f"  {'TOTAL':<10} {total:>4}")
    out()

    rule("Rate limits (today)")
    for channel, usage in summary["daily_usage"].items():
        out(f"  {channel:<6} {usage['used']:>3}/{usage['limit']:<3} used, {usage['remaining']:>3} remaining")
    out()

    if content and "error" not in content:
        rule("Content syndication")
        by_status = content.get("by_status") or {}
        for status in CONTENT_STATUSES:
            out(f"  {status:<12} {by_status.get(status, 0):>4}")
        out(f"  {'pending pairs':<12} {content.get('pending_targets', 0):>4}")
        out(f"  {'live URLs':<12} {len(content.get('live_urls') or []):>4}")
        out()
        for platform, usage in (content.get("daily_usage") or {}).items():
            if usage["limit"]:
                out(f"  {platform:<12} {usage['used']:>3}/{usage['limit']:<3} used, "
                    f"{usage['remaining']:>3} remaining")
        not_ready = [entry for entry in check_all(config, dry_run=True) if not entry.get("ready")]
        if not_ready:
            out()
            out("  Not ready:")
            for entry in not_ready:
                out(f"    {entry['platform']:<12} {entry.get('reason') or 'not ready'}")
        out()

    rule("Next up")
    queue = tracker.next_batch(described["batch_size"])
    if not queue:
        out("  (nothing - run 'python main.py discover' to find prospects)")
    cooldown = config.int("rate_limits.min_hours_between_attempts", 0)
    senders: Dict[str, Any] = {}
    for sponsor in queue:
        gate, reason = tracker.can_send(sponsor, min_hours_between_attempts=cooldown)
        if gate:
            try:
                if sponsor.channel not in senders:
                    senders[sponsor.channel] = get_sender(sponsor.channel, config, dry_run=True)
                deliverable, deliver_reason = senders[sponsor.channel].can_send(sponsor)
                gate, reason = (True, deliver_reason) if deliverable else (False, deliver_reason)
            except Exception as exc:  # noqa: BLE001
                gate, reason = False, f"channel unavailable: {exc}"
        flag = "->" if gate else "xx"
        out(f"  {flag} {sponsor.name:<28} {sponsor.channel:<6} {sponsor.target() or 'no contact':<34} {reason}")
    out()

    if summary["recent_history"]:
        rule("Recent activity")
        for entry in summary["recent_history"]:
            detail = " ".join(f"{key}={value}" for key, value in entry.items() if key not in {"at", "event"})
            out(f"  {entry.get('at', '')}  {entry.get('event', ''):<8} {detail}")
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    config = load_config(args)
    tracker = build_tracker(config, args)
    statuses = [args.status] if args.status else None
    sponsors = tracker.all(statuses=statuses, channel=args.channel)

    if args.json:
        emit_json([sponsor.to_dict() for sponsor in sponsors])
        return EXIT_OK

    if not sponsors:
        out("No sponsors tracked. Try: python main.py discover --yes")
        return EXIT_OK

    rule(f"{len(sponsors)} sponsor(s)")
    header = f"{'#':>3}  {'NAME':<28} {'CHANNEL':<7} {'STATUS':<10} {'STARS':>5}  {'CONTACT':<34} NOTES"
    out(header)
    out("-" * len(header))
    for index, sponsor in enumerate(sponsors, start=1):
        contact = sponsor.target() or "-"
        notes = (sponsor.notes or "")[:60]
        out(
            f"{index:>3}  {sponsor.name[:28]:<28} {sponsor.channel:<7} {sponsor.status:<10} "
            f"{sponsor.stars:>5}  {contact[:34]:<34} {notes}"
        )
    return EXIT_OK


def cmd_mark(args: argparse.Namespace) -> int:
    config = load_config(args)
    log = get_logger("cli")
    tracker = build_tracker(config, args)
    try:
        sponsor = tracker.update_status(args.name, args.status, note=args.note or "")
    except SponsorNotFound:
        log.error("Sponsor %r is not in the tracker", args.name)
        return EXIT_ERROR
    except TrackerError as exc:
        log.error("%s", exc)
        return EXIT_USAGE
    tracker.save()
    out(f"{sponsor.name} -> {sponsor.status}")
    return EXIT_OK


def cmd_remove(args: argparse.Namespace) -> int:
    config = load_config(args)
    log = get_logger("cli")
    tracker = build_tracker(config, args)
    sponsor = tracker.get(args.name)
    if sponsor is None:
        log.error("Sponsor %r is not in the tracker", args.name)
        return EXIT_ERROR
    if not args.yes:
        if not sys.stdin.isatty():
            log.error("Refusing to remove without --yes in a non-interactive session")
            return EXIT_USAGE
        answer = input(f"Remove {sponsor.name} ({sponsor.status})? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            out("Cancelled")
            return EXIT_OK
    tracker.remove(args.name)
    tracker.save()
    out(f"Removed {sponsor.name}")
    return EXIT_OK


def cmd_ui(args: argparse.Namespace) -> int:
    from webapp.server import run_ui

    return run_ui(
        base_dir=args.base_dir,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )


# --------------------------------------------------------------------------- #
# Content syndication
# --------------------------------------------------------------------------- #


def _selected_platforms(config: Config, raw: Optional[str]) -> List[str]:
    """Resolve `--platform` into a validated id list ([] means all enabled)."""
    if not raw:
        return []
    chosen: List[str] = []
    for token in str(raw).replace(",", " ").split():
        platform = token.strip().lower()
        if platform not in PLATFORM_IDS:
            log = get_logger("cli")
            log.error(
                "Unknown platform %r; expected a comma separated subset of: %s",
                platform,
                ", ".join(PLATFORM_IDS),
            )
            raise SystemExit(EXIT_USAGE)
        if platform not in chosen:
            chosen.append(platform)
    return chosen


def build_promoter(config: Config, args: argparse.Namespace):
    """Compose a Promoter, including the optional external renderer."""
    from promoter import Promoter, build_store

    renderer = _renderer_for(config, getattr(args, "llm", False))
    return Promoter(
        config,
        build_store(config),
        dry_run=True if getattr(args, "dry_run", False) else None,
        batch_size=getattr(args, "batch", None),
        llm_renderer=renderer,
    )


def _renderer_for(config: Config, use_llm: bool):
    """Shared LLM-renderer wiring for `draft` and `publish`."""
    if not use_llm:
        return None
    from llm import LLMError, SubprocessLLM

    command = config.list("scheduler.llm_command")
    if not command:
        raise ConfigError(
            "--llm requested but scheduler.llm_command is empty. "
            'Set it to a local CLI, e.g. ["ollama", "run", "llama3.1"].'
        )
    try:
        renderer = SubprocessLLM(
            command,
            timeout=config.int("scheduler.llm_timeout_seconds", 120, minimum=5, maximum=900),
        )
    except LLMError as exc:
        raise ConfigError(f"--llm requested but {exc}") from exc
    if not renderer.exists():
        raise ConfigError(f"--llm requested but {renderer.describe()!r} is not on PATH")
    get_logger("cli").info("Using external renderer: %s", renderer.describe())
    return renderer


def cmd_platforms(args: argparse.Namespace) -> int:
    """Readiness report for every syndication target. Never transmits."""
    from publishers import check_all

    config = load_config(args)
    log = get_logger("cli")
    wanted = _selected_platforms(config, args.platform)
    report = check_all(config, dry_run=True, platforms=wanted or None)

    probe: Dict[str, Any] = {}
    if args.probe:
        from publishers import get_publisher

        publisher = get_publisher(args.probe, config, dry_run=True)
        if hasattr(publisher, "discover_publications"):
            probe = {"publications": publisher.discover_publications()}
        elif hasattr(publisher, "fetch_identity"):
            probe = {"identity": publisher.fetch_identity()}
        else:
            log.info("%s has nothing to probe", args.probe)

    if args.json:
        emit_json({"platforms": report, "enabled": config.platforms_enabled(), "probe": probe})
        return EXIT_OK

    rule("Syndication platforms")
    out(f"{'':<12} {'KIND':<8} {'READY':<6} LABEL")
    out("-" * 78)
    for entry in report:
        enabled = "yes" if entry.get("enabled") else "no"
        ready = "yes" if entry.get("ready") else "no"
        label = str(entry.get("label") or entry.get("platform"))
        out(f"{entry['platform']:<12} {str(entry.get('kind') or '-'):<8} {ready:<6} {label}")
        out(f"{'':<12} enabled={enabled} mode={entry.get('mode') or '-'}")
        if entry.get("reason") and entry["reason"] != "ok":
            out(f"{'':<12} -> {entry['reason']}")
        if entry.get("legacy"):
            out(f"{'':<12} !! legacy target, see the note below")
        out()

    enabled = config.platforms_enabled()
    out(f"Enabled platforms : {', '.join(enabled) or '(none)'}")
    out(f"Content enabled   : {'yes' if config.publishing_enabled else 'no'}")
    out(f"Content ledger    : {config.path('paths.content_file', 'content.json')}")
    out(f"Outbox directory  : {config.path('publishing.outbox_dir', 'content_outbox')}")

    for entry in report:
        if entry.get("notes"):
            out()
            out(f"{entry['platform']}: {entry['notes']}")
        if entry.get("token_env") and not entry.get("token_set"):
            out(f"  {entry['platform']}: set ${entry['token_env']} before publishing")
        if entry.get("token_help"):
            out(f"  get a credential at: {entry['token_help']}")

    if probe:
        rule("Probe")
        out(json.dumps(probe, indent=2, default=str))

    if not enabled:
        out()
        out("Nothing is enabled yet. Add to config.yaml, for example:")
        out("  platforms:")
        out("    devto:")
        out("      enabled: true")
        out("    devdojo:")
        out("      enabled: true")
        out("  content:")
        out("    enabled: true")
    return EXIT_OK


def cmd_draft(args: argparse.Namespace) -> int:
    """Compose an article and store it as a draft. Transmits nothing."""
    from content_engine import content_data_from_config, generate, render_prompt
    from content_store import ContentStore, ContentStoreError
    from promoter import build_store

    config = load_config(args)
    log = get_logger("cli")
    data = content_data_from_config(config)
    store = build_store(config)

    platforms = _selected_platforms(config, args.platform) or config.platforms_enabled()
    if not platforms:
        log.warning(
            "No platforms enabled - the draft will be stored with no targets. "
            "Run 'python main.py platforms' to see how to enable one."
        )

    if args.prompt:
        out(
            render_prompt(
                data,
                title=args.title or "",
                topic=args.topic or "",
                platform=platforms[0] if platforms else "",
            )
        )
        return EXIT_OK

    try:
        if args.from_file:
            stored = _import_markdown(store, args.from_file, platforms, log)
        else:
            renderer = _renderer_for(config, args.llm)
            composed = generate(
                data,
                title=args.title or "",
                topic=args.topic or "",
                platform=platforms[0] if platforms else "",
                llm_renderer=renderer,
            )
            item = ContentItem(
                id=ContentStore.make_id(composed["title"], args.topic or ""),
                title=composed["title"],
                body_markdown=composed["body"],
                summary=composed["summary"],
                topic=args.topic or "",
                platforms=platforms,
                tags=[str(tag) for tag in (data.get("tags") or [])],
                canonical_url=str(data.get("canonical_base_url") or ""),
                status="draft",
                source="composed",
                words=int(composed.get("words") or 0),
            )
            item.fingerprint = item.compute_fingerprint()
            stored, _created = store.add(item)
            store.save()
            renderer_label = composed.get("renderer") or "deterministic"
    except (ContentStoreError, ConfigError) as exc:
        log.error("%s", exc)
        return EXIT_ERROR

    problems = _gate_for_all(data, stored, platforms)
    payload = {
        "id": stored.id,
        "title": stored.title,
        "status": stored.status,
        "words": stored.words,
        "platforms": stored.platforms,
        "tags": stored.tags,
        "renderer": renderer_label,
        "problems": problems,
        "content_file": str(store.path),
    }

    if args.json:
        emit_json(payload)
        return EXIT_ERROR if problems else EXIT_OK

    rule(f"Draft {stored.id}")
    out(f"Title     : {stored.title}")
    out(f"Status    : {stored.status} (approve it before publishing)")
    out(f"Words     : {stored.words}")
    out(f"Renderer  : {renderer_label}")
    out(f"Targets   : {', '.join(stored.platforms) or '(none)'}")
    out(f"Tags      : {', '.join(stored.tags) or '(none)'}")
    out(f"Stored in : {store.path}")
    out()
    if args.show:
        out(stored.body_markdown)
        out()
    if problems:
        rule("Quality gate")
        for problem in problems:
            out(f"  - {problem}")
        out()
        out("Publishing will refuse this draft until the findings are fixed.")
        out("Enrich plugin.features / content.angle, or render with --llm.")
        out("To override, set publishing.enforce_quality_gate: false - and accept")
        out("that thin or undisclosed posts get removed by community moderators.")
        return EXIT_ERROR
    if not args.show:
        out(f"Preview with: python main.py content show {stored.id}")
        out()
    out(f"Approve it: python main.py content approve {stored.id} --yes")
    return EXIT_OK


def _gate_for_all(data: Dict[str, Any], item: ContentItem, platforms: Sequence[str]) -> List[str]:
    """Union of the per-platform gate findings for one draft."""
    from content_engine import validate

    problems: List[str] = []
    for platform in platforms or [""]:
        found = validate(
            data,
            title=item.title,
            body=item.body_markdown,
            summary=item.summary,
            platform=platform,
        )
        for problem in found:
            text = f"[{platform or 'generic'}] {problem}" if platform else problem
            if text not in problems:
                problems.append(text)
    return problems


def _import_markdown(store, path: str, platforms: List[str], log) -> ContentItem:  # noqa: ANN001
    """Load a hand-written markdown file as a draft and return the stored item."""
    from content_store import ContentStore
    from publishers.markdown_html import strip_front_matter

    source = Path(path).expanduser()
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {source}: {exc}") from exc
    _front, body = strip_front_matter(raw)
    body = body.strip()
    if not body:
        raise ConfigError(f"{source} has no body after stripping front matter")

    title = ""
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            body = body.split("\n", 1)[1] if "\n" in body else ""
            break
    if not title:
        title = source.stem.replace("-", " ").replace("_", " ").strip()

    item = ContentItem(
        id=ContentStore.make_id(title),
        title=title,
        body_markdown=body.strip(),
        topic=source.stem,
        platforms=platforms,
        status="draft",
        source="imported",
        words=len(body.split()),
    )
    item.fingerprint = item.compute_fingerprint()
    stored, _created = store.add(item)
    store.save()
    log.info("Imported %s as content item %s", source, stored.id)
    return stored


def cmd_publish(args: argparse.Namespace) -> int:
    """Publish approved articles to their platforms."""
    config = load_config(args)
    log = get_logger("cli")

    if not config.publishing_enabled:
        log.error(
            "Content syndication is off. Set content.enabled: true and at least one "
            "platforms.<id>.enabled: true (see 'python main.py platforms')."
        )
        return EXIT_USAGE

    try:
        promoter = build_promoter(config, args)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_USAGE

    platforms = _selected_platforms(config, args.platform)
    unknown = [pid for pid in platforms if pid not in config.platforms_enabled()]
    for platform in unknown:
        log.warning("platforms.%s.enabled is false - it will be skipped", platform)

    if args.dry_run:
        log.warning("DRY RUN: articles are shaped and checked, nothing is transmitted")

    out(f"Content    : {config.path('paths.content_file', 'content.json')}")
    out(f"Mode       : {'DRY RUN' if promoter.dry_run else 'LIVE'}")
    out(f"Platforms  : {', '.join(platforms or promoter.platforms) or '(none)'}")
    out(f"Batch      : {promoter.batch_size}")
    out(f"Usage today: {promoter.store.daily_usage()}")
    out()

    if args.once:
        result = promoter.tick(
            platforms=platforms or None,
            item_ids=[args.id] if args.id else None,
        )
        promoter.store.save()
        out()
        rule("Publish tick")
        out(
            f"attempted={result.attempted} published={result.published} "
            f"drafted={result.drafted} queued={result.queued} "
            f"failed={result.failed} skipped={result.skipped}"
        )
        for record in result.records:
            flag = "OK " if record.ok else "ERR"
            state = record.url or record.detail
            out(f"  [{flag}] {record.item_id:<26} {record.platform:<12} {record.words:>4}w  {state}")
        for note in result.notes:
            out(f"  note: {note}")
        if result.queued:
            out()
            from content_engine import approve_hint

            out(approve_hint())
        if args.json:
            out()
            out(json.dumps(result.to_dict(), indent=2, default=str))
        return EXIT_OK if result.failed == 0 else EXIT_ERROR

    results = promoter.run_forever(
        interval=args.interval, max_ticks=args.max_ticks, platforms=platforms or None
    )
    promoter.store.save()
    total = sum(record.ok for result in results for record in result.records)
    out(f"Published {total} article/platform pair(s) across {len(results)} tick(s)")
    return EXIT_OK


def cmd_content(args: argparse.Namespace) -> int:
    """Review and curate the content ledger."""
    handler = CONTENT_COMMANDS.get(args.content_command)
    if handler is None:
        print(f"Unknown content command: {args.content_command}")
        return EXIT_USAGE
    return handler(args)


def content_store_for(args: argparse.Namespace):
    """Load the config and the campaign's ContentStore."""
    from promoter import build_store

    config = load_config(args)
    return config, build_store(config)


def content_list(args: argparse.Namespace) -> int:
    config, store = content_store_for(args)
    items = store.all(statuses=[args.status] if args.status else None, platform=args.platform)
    summary = store.summary()

    if args.json:
        emit_json(
            {
                "items": [item.to_dict() for item in items],
                "summary": summary,
                "content_file": str(store.path),
            }
        )
        return EXIT_OK

    rule(f"{len(items)} article(s)")
    if not items:
        out("Nothing drafted yet. Try: python main.py draft --topic \"...\"")
        out(f"Ledger: {store.path}")
        return EXIT_OK

    for item in items:
        out()
        out(f"  {item.id}  [{item.status}]  {item.words}w")
        out(f"    title      {item.title}")
        out(f"    targets    {', '.join(item.platforms) or '(none)'}")
        for platform in item.platforms:
            entry = item.publications.get(platform)
            if entry is None:
                out(f"      {platform:<12} pending")
            else:
                suffix = entry.url or entry.detail or entry.last_error or ""
                out(f"      {platform:<12} {entry.status:<8} {suffix}")
        if item.last_error:
            out(f"    last error {item.last_error}")

    out()
    rule("Content summary")
    for status, count in summary["by_status"].items():
        out(f"  {status:<12} {count:>4}")
    out(f"  {'pending pairs':<12} {summary['pending_targets']:>4}")
    if summary["live_urls"]:
        out()
        rule("Live URLs")
        for url in summary["live_urls"]:
            out(f"  {url}")
    out()
    rule("Rate limits today")
    for platform, usage in summary["daily_usage"].items():
        out(f"  {platform:<12} {usage['used']:>3}/{usage['limit']:<3} used, {usage['remaining']:>3} left")
    return EXIT_OK


def content_show(args: argparse.Namespace) -> int:
    from content_engine import content_data_from_config, prepare_for_platform

    config, store = content_store_for(args)
    item = store.require(args.id)
    if args.json:
        emit_json(item.to_dict())
        return EXIT_OK

    rule(f"{item.id}  [{item.status}]")
    out(f"Title   : {item.title}")
    out(f"Topic   : {item.topic or '-'}")
    out(f"Words   : {item.words}")
    out(f"Targets : {', '.join(item.platforms)}")
    out(f"Tags    : {', '.join(item.tags) or '-'}")
    out(f"Source  : {item.source}")
    if item.canonical_url:
        out(f"Canonical: {item.canonical_url}")
    out()
    out(item.body_markdown)
    out()
    rule("Per-platform shaping")
    data = content_data_from_config(config)
    for platform in item.platforms:
        payload = prepare_for_platform(
            data,
            title=item.title,
            body=item.body_markdown,
            summary=item.summary,
            tags=item.tags,
            platform=platform,
        )
        verdict = "publishable" if payload["publishable"] else "BLOCKED"
        out(f"  {platform:<12} {verdict:<12} {payload['words']}w  tags={','.join(payload['tags']) or '-'}")
        for problem in payload["problems"]:
            out(f"      - {problem}")
    return EXIT_OK


def content_add(args: argparse.Namespace) -> int:

    config, store = content_store_for(args)
    platforms = _selected_platforms(config, args.platform) or config.platforms_enabled()
    if not platforms:
        get_logger("cli").warning("no platforms enabled - the article will have no targets")

    try:
        if args.from_file:
            stored = _import_markdown(store, args.from_file, platforms, get_logger("cli"))
            created = True
        else:
            body = args.body
            if args.body_file:
                body = Path(args.body_file).expanduser().read_text(encoding="utf-8")
            if not body or not body.strip():
                print("Provide --body or --body-file.")
                return EXIT_USAGE
            item = ContentItem(
                id=slugify(args.id or args.title),
                title=args.title,
                body_markdown=body.strip(),
                summary=args.summary or "",
                topic=args.topic or "",
                platforms=platforms,
                tags=[tag.strip() for tag in (args.tags or "").split(",") if tag.strip()],
                status="draft",
                source="manual",
                words=len(body.split()),
            )
            item.fingerprint = item.compute_fingerprint()
            stored, created = store.add(item)
            store.save()
    except (ContentStoreError, ConfigError, OSError) as exc:
        print(f"Could not add the article: {exc}")
        return EXIT_ERROR

    print(("Added " if created else "Updated ") + stored.summary_line())
    print(f"Approve it with: python main.py content approve {stored.id} --yes")
    return EXIT_OK


def content_approve(args: argparse.Namespace) -> int:
    _config, store = content_store_for(args)
    item = store.approve(args.id)
    if args.yes or sys.stdin.isatty():
        if not args.yes:
            answer = input(f"Approve and publish '{item.title}'? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                print("Cancelled.")
                return EXIT_OK
    else:
        print("Refusing to approve in a non-interactive session without --yes")
        return EXIT_USAGE
    store.save()
    pending = item.pending_platforms()
    print(f"{item.id} -> approved")
    print(f"Pending targets: {', '.join(pending) or '(none)'}")
    if pending:
        print("Publish with: python main.py publish --once --dry-run")
    return EXIT_OK


def content_mark(args: argparse.Namespace) -> int:
    from content_store import ContentNotFound, ContentStoreError

    _config, store = content_store_for(args)
    try:
        item = store.update_status(args.id, args.status, note=args.note or "")
    except ContentNotFound:
        print(f"Article {args.id!r} is not in the ledger")
        return EXIT_ERROR
    except ContentStoreError as exc:
        print(f"{exc}")
        return EXIT_USAGE
    store.save()
    print(f"{item.id} -> {item.status}")
    return EXIT_OK


def content_confirm(args: argparse.Namespace) -> int:
    from content_store import ContentNotFound, ContentStoreError

    _config, store = content_store_for(args)
    try:
        item = store.confirm(args.id, args.platform, url=args.url or "")
    except ContentNotFound:
        print(f"Article {args.id!r} is not in the ledger")
        return EXIT_ERROR
    except ContentStoreError as exc:
        print(f"{exc}")
        return EXIT_USAGE
    store.save()
    entry = item.publication(args.platform)
    print(f"Confirmed {item.id} on {args.platform}")
    print(f"  {entry.status}  {entry.url}")
    if item.status == "published":
        print("Every target is live - this article is fully syndicated.")
    else:
        remaining = item.pending_platforms()
        print(f"Still pending: {', '.join(remaining) or '(none)'}")
    return EXIT_OK


def content_reset(args: argparse.Namespace) -> int:
    from content_store import ContentNotFound, ContentStoreError

    _config, store = content_store_for(args)
    try:
        item = store.unconfirm(args.id, args.platform)
    except ContentNotFound:
        print(f"Article {args.id!r} is not in the ledger")
        return EXIT_ERROR
    except ContentStoreError as exc:
        print(f"{exc}")
        return EXIT_USAGE
    store.save()
    print(f"Reset {item.id} on {args.platform} - it will be offered again")
    return EXIT_OK


def content_remove(args: argparse.Namespace) -> int:
    from content_store import ContentNotFound

    _config, store = content_store_for(args)
    try:
        item = store.require(args.id)
    except ContentNotFound:
        print(f"Article {args.id!r} is not in the ledger")
        return EXIT_ERROR
    if not args.yes:
        if not sys.stdin.isatty():
            print("Refusing to remove without --yes in a non-interactive session")
            return EXIT_USAGE
        if input(f"Remove {item.id} ({item.status})? [y/N]: ").strip().lower() not in {"y", "yes"}:
            print("Cancelled")
            return EXIT_OK
    store.remove(args.id)
    store.save()
    print(f"Removed {item.id}")
    return EXIT_OK


CONTENT_COMMANDS = {
    "list": content_list,
    "show": content_show,
    "add": content_add,
    "approve": content_approve,
    "mark": content_mark,
    "confirm": content_confirm,
    "reset": content_reset,
    "remove": content_remove,
}


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #


def load_user_store(args: argparse.Namespace) -> UserStore:
    base = Path(args.base_dir).expanduser() if args.base_dir else Path.cwd()
    return UserStore(base / USERS_FILENAME)


def ask(prompt: str, *, default: str = "") -> str:
    """Prompt for a value, mirroring the non-interactive guard used elsewhere."""
    if not sys.stdin.isatty():
        return default
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        out()
        return default


def prompt_password(prompt: str = "Password: ", *, confirm: bool = False) -> str:
    """Read a password without echo. Returns "" when it cannot be read."""
    if not sys.stdin.isatty():
        print("Passwords can only be entered interactively; run this from a terminal.")
        return ""
    try:
        password = getpass.getpass(prompt)
        if confirm and getpass.getpass("Confirm: ") != password:
            print("Passwords do not match.")
            return ""
    except (EOFError, KeyboardInterrupt):
        out()
        return ""
    return password


def require_email(args: argparse.Namespace) -> str:
    email = normalize_email(args.email or ask("Email: "))
    if not email:
        print("An email address is required (pass --email).")
        raise SystemExit(EXIT_USAGE)
    return email


def confirm(question: str) -> bool:
    """Yes/no gate. Declining is a success, not an error."""
    if ask(question) in {"y", "yes"}:
        return True
    print("Cancelled.")
    return False


def cmd_auth(args: argparse.Namespace) -> int:
    setup_logging(level=args.log_level or "INFO", quiet=True)
    try:
        store = load_user_store(args)
    except AuthError as exc:
        print(str(exc))
        return EXIT_ERROR
    handler = AUTH_COMMANDS.get(args.auth_command)
    if handler is None:
        print(f"Unknown auth command: {args.auth_command}")
        return EXIT_USAGE
    try:
        return handler(store, args)
    except AuthError as exc:
        print(str(exc))
        return EXIT_ERROR


def auth_adduser(store: UserStore, args: argparse.Namespace) -> int:
    email = require_email(args)
    password = args.password or prompt_password(confirm=True)
    if not password:
        return EXIT_USAGE
    store.add(email, password, confirmation=args.password or password)
    rule("account created")
    for key, value in store.describe(email).items():
        out(f"  {key:<20} {value}")
    out("")
    out("Next: sign in and switch on two-factor, or check it here with")
    out(f"  python main.py auth totp --email {email}")
    return EXIT_OK


def auth_list(store: UserStore, args: argparse.Namespace) -> int:
    if args.json:
        emit_json([store.describe(email) for email in store.emails()])
        return EXIT_OK
    rule("web ui accounts")
    if not len(store):
        out("  No accounts yet - the browser will offer to create one.")
    for email in store.emails():
        described = store.describe(email)
        out(f"  {email}")
        out(f"    two-factor        {'enrolled' if described['totp_enrolled'] else 'not enrolled'}")
        out(f"    recovery codes    {described['recovery_codes_left']}")
        out(f"    created           {described['created_at'] or '-'}")
        out(f"    last sign-in      {described['last_login'] or 'never'}")
    rule("")
    info = describe_store(store)
    out(f"  file                {info['file']}")
    out(f"  {MASTER_ENV}  {'set' if info['master_env_set'] else 'NOT SET (weaker key handling)'}")
    return EXIT_OK


def auth_passwd(store: UserStore, args: argparse.Namespace) -> int:
    email = require_email(args)
    password = prompt_password(confirm=True)
    if not password:
        return EXIT_USAGE
    store.set_password(email, password, confirmation=password)
    print(f"Password updated for {email}")
    return EXIT_OK


def auth_remove(store: UserStore, args: argparse.Namespace) -> int:
    email = require_email(args)
    if store.get(email) is None:
        print(f"No such account: {email}")
        return EXIT_ERROR
    if not args.yes and not confirm(f"Delete the account {email}? [y/N] "):
        return EXIT_OK
    store.remove(email)
    print(f"Removed {email}")
    return EXIT_OK


def auth_totp(store: UserStore, args: argparse.Namespace) -> int:
    email = require_email(args)
    if not store.has_totp(email):
        print(f"Two-factor is not enrolled for {email}; nothing to do.")
        return EXIT_OK
    if not args.yes and not confirm(f"Show the two-factor status for {email}? [y/N] "):
        return EXIT_OK
    out("Two-factor is enrolled. Sign in to see your recovery codes.")
    out("To add a new device, turn it off from Security & 2FA in the browser,")
    out(f"or run: python main.py auth totp-disable --email {email}")
    return EXIT_OK


def auth_totp_disable(store: UserStore, args: argparse.Namespace) -> int:
    email = require_email(args)
    if not store.has_totp(email):
        print(f"Two-factor is not enrolled for {email}.")
        return EXIT_OK
    if not args.yes and not confirm(f"Turn off two-factor for {email}? [y/N] "):
        return EXIT_OK
    store.disable_totp(email)
    print(f"Two-factor disabled for {email}")
    return EXIT_OK


def auth_recovery(store: UserStore, args: argparse.Namespace) -> int:
    email = require_email(args)
    if not store.has_totp(email):
        print(f"Two-factor is not enrolled for {email}; nothing to do.")
        return EXIT_OK
    codes = generate_recovery_codes()
    store.set_recovery_codes(email, codes)
    rule("new recovery codes")
    out("  Each code works once, in place of your authenticator.")
    out("  They are shown only now - only their hashes are stored.")
    for code in codes:
        out(f"    {code}")
    return EXIT_OK


AUTH_COMMANDS = {
    "adduser": auth_adduser,
    "list": auth_list,
    "passwd": auth_passwd,
    "remove": auth_remove,
    "totp": auth_totp,
    "totp-disable": auth_totp_disable,
    "recovery": auth_recovery,
}


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


PLATFORM_HELP = "comma separated subset of: " + ", ".join(PLATFORM_IDS)


def _add_content_parsers(sub: Any) -> None:
    """Register the content-syndication command group.

    Kept out of `build_parser` so the tree stays readable and so adding a
    platform id in `models.py` propagates to the help text automatically.
    """
    p_platforms = sub.add_parser(
        "platforms",
        help="syndication targets: what is configured and whether it is ready",
    )
    p_platforms.add_argument("--platform", help=PLATFORM_HELP)
    p_platforms.add_argument(
        "--probe",
        choices=list(PLATFORM_IDS),
        help="call a read-only endpoint to discover ids (e.g. hashnode publications)",
    )
    p_platforms.add_argument("--json", action="store_true")
    p_platforms.set_defaults(func=cmd_platforms)

    p_draft = sub.add_parser(
        "draft", help="compose an article into content.json (transmits nothing)"
    )
    p_draft.add_argument("--title", help="article title (default: derived from config)")
    p_draft.add_argument("--topic", help="narrow the article to one angle or topic")
    p_draft.add_argument("--platform", help=f"targets to attach (default: enabled ones). {PLATFORM_HELP}")
    p_draft.add_argument("--from-file", help="import a hand-written markdown file instead")
    p_draft.add_argument("--show", action="store_true", help="print the article body")
    p_draft.add_argument("--prompt", action="store_true", help="print the LLM prompt and exit")
    p_draft.add_argument("--llm", action="store_true", help="render via scheduler.llm_command")
    p_draft.add_argument("--json", action="store_true")
    p_draft.set_defaults(func=cmd_draft)

    p_pub = sub.add_parser(
        "publish", help="publish approved articles to their target platforms"
    )
    p_pub.add_argument("--once", action="store_true", help="run a single tick and exit")
    p_pub.add_argument("--dry-run", action="store_true", help="shape and check, transmit nothing")
    p_pub.add_argument("--platform", help=f"restrict to these targets. {PLATFORM_HELP}")
    p_pub.add_argument("--id", help="publish only this content item")
    p_pub.add_argument("--batch", type=int, help="(article, platform) pairs per tick")
    p_pub.add_argument("--interval", type=int, help="seconds between ticks (default: from config)")
    p_pub.add_argument("--max-ticks", type=int, help="stop after N ticks (testing)")
    p_pub.add_argument("--llm", action="store_true", help="render via scheduler.llm_command")
    p_pub.add_argument("--json", action="store_true", help="JSON tick result with --once")
    p_pub.set_defaults(func=cmd_publish)

    p_content = sub.add_parser("content", help="review and curate the content ledger")
    content_sub = p_content.add_subparsers(dest="content_command", metavar="SUBCOMMAND")

    c_list = content_sub.add_parser("list", help="every article and its publication ledger")
    c_list.add_argument("--status", choices=CONTENT_STATUSES)
    c_list.add_argument("--platform", choices=list(PLATFORM_IDS))
    c_list.add_argument("--json", action="store_true")
    c_list.set_defaults(func=content_list)

    c_show = content_sub.add_parser("show", help="print one article and its per-platform shaping")
    c_show.add_argument("id")
    c_show.add_argument("--json", action="store_true")
    c_show.set_defaults(func=content_show)

    c_add = content_sub.add_parser("add", help="add or replace an article by hand")
    c_add.add_argument("--title", required=True)
    c_add.add_argument("--id", help="stable id (default: derived from the title)")
    c_add.add_argument("--body", help="markdown body")
    c_add.add_argument("--body-file", help="read the markdown body from this file")
    c_add.add_argument("--summary")
    c_add.add_argument("--topic")
    c_add.add_argument("--tags", help="comma separated")
    c_add.add_argument("--platform", help=PLATFORM_HELP)
    c_add.add_argument("--from-file", help="import a markdown file (title taken from its H1)")
    c_add.set_defaults(func=content_add)

    c_approve = content_sub.add_parser("approve", help="approve a draft for publishing")
    c_approve.add_argument("id")
    c_approve.add_argument("--yes", action="store_true", help="do not prompt")
    c_approve.set_defaults(func=content_approve)

    c_mark = content_sub.add_parser("mark", help="change an article's status")
    c_mark.add_argument("id")
    c_mark.add_argument("--status", required=True, choices=CONTENT_STATUSES)
    c_mark.add_argument("--note")
    c_mark.set_defaults(func=content_mark)

    c_confirm = content_sub.add_parser(
        "confirm", help="record the live URL for a manually submitted article"
    )
    c_confirm.add_argument("id")
    c_confirm.add_argument("--platform", required=True, choices=list(PLATFORM_IDS))
    c_confirm.add_argument("--url", required=True, help="the published http(s) URL")
    c_confirm.set_defaults(func=content_confirm)

    c_reset = content_sub.add_parser(
        "reset", help="clear a publication so the pair is offered again"
    )
    c_reset.add_argument("id")
    c_reset.add_argument("--platform", required=True, choices=list(PLATFORM_IDS))
    c_reset.set_defaults(func=content_reset)

    c_remove = content_sub.add_parser("remove", help="delete an article from the ledger")
    c_remove.add_argument("id")
    c_remove.add_argument("--yes", action="store_true")
    c_remove.set_defaults(func=content_remove)

    p_content.set_defaults(func=cmd_content)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Promote an open-source project two ways: targeted sponsor outreach, "
            "and content syndication to developer communities."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Outreach examples:\n"
            "  python main.py discover --yes --channel email\n"
            "  python main.py add-sponsor --name 'Acme ISV' --email devrel@acme.com\n"
            "  python main.py generate --sponsor Acme --channel forum\n"
            "  python main.py run --once --dry-run\n"
            "\n"
            "Content syndication examples:\n"
            "  python main.py platforms\n"
            "  python main.py draft --topic 'retry policies for contact-centre APIs'\n"
            "  python main.py content approve <id> --yes\n"
            "  python main.py publish --once --dry-run\n"
            "  python main.py content confirm <id> --platform devdojo --url <url>\n"
            "\n"
            "  python main.py status\n"
            "  python main.py ui\n"
        ),
    )
    parser.add_argument("--config", help="path to config.yaml (default: ./config.yaml)")
    parser.add_argument("--tracker", help="override paths.tracker_file")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    parser.add_argument("--quiet", action="store_true", help="console: errors only (bot.log unaffected)")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # discover
    p_discover = sub.add_parser("discover", help="scrape GitHub topics for new prospects")
    p_discover.add_argument("--topics", help="comma separated topics (default: from config)")
    p_discover.add_argument("--channel", choices=CHANNELS, default="email", help="channel for new sponsors")
    p_discover.add_argument("--add", help="add these candidates: 1,3-5 or all")
    p_discover.add_argument("--yes", action="store_true", help="add every new candidate without prompting")
    p_discover.add_argument("--limit", type=int, help="cap how many candidates may be selected")
    p_discover.add_argument("--no-emails", action="store_true", help="skip public email lookup")
    p_discover.add_argument("--json", action="store_true", help="machine readable output")
    p_discover.add_argument("--no-input", action="store_true", help="never prompt")
    p_discover.set_defaults(func=cmd_discover)

    # add-sponsor
    p_add = sub.add_parser("add-sponsor", help="add or update a sponsor manually")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--email")
    p_add.add_argument("--channel", choices=CHANNELS, default="email")
    p_add.add_argument("--status", choices=STATUSES, default="new")
    p_add.add_argument("--contact", help="forum handle, thread URL or contact form")
    p_add.add_argument("--company")
    p_add.add_argument("--github", help="GitHub login (used to find a public email)")
    p_add.add_argument("--website")
    p_add.add_argument("--notes")
    p_add.add_argument("--no-email", action="store_true", help="allow an empty email address")
    p_add.set_defaults(func=cmd_add_sponsor)

    # generate
    p_gen = sub.add_parser("generate", help="dry-run: render a message, send nothing")
    p_gen.add_argument("--sponsor", help="sponsor name (default: next in queue)")
    p_gen.add_argument("--channel", choices=CHANNELS, help="default: the sponsor's channel")
    p_gen.add_argument("--llm", action="store_true", help="render via scheduler.llm_command")
    p_gen.add_argument("--prompt", action="store_true", help="print the rendered LLM prompt instead")
    p_gen.add_argument("--out", help="also write the message to this file")
    p_gen.add_argument("--json", action="store_true")
    p_gen.set_defaults(func=cmd_generate)

    # run
    p_run = sub.add_parser("run", help="start the scheduler")
    p_run.add_argument("--once", action="store_true", help="run a single tick and exit")
    p_run.add_argument("--dry-run", action="store_true", help="never transmit")
    p_run.add_argument("--batch", type=int, help="sponsors per tick (default: from config)")
    p_run.add_argument("--interval", type=int, help="seconds between ticks (default: from config)")
    p_run.add_argument("--max-ticks", type=int, help="stop after N ticks (testing)")
    p_run.add_argument("--llm", action="store_true", help="render via scheduler.llm_command")
    p_run.add_argument("--json", action="store_true", help="JSON tick result with --once")
    p_run.set_defaults(func=cmd_run)

    # status
    p_status = sub.add_parser("status", help="pipeline summary and next-up queue")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    # list
    p_list = sub.add_parser("list", help="every tracked sponsor")
    p_list.add_argument("--status", choices=STATUSES)
    p_list.add_argument("--channel", choices=CHANNELS)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    # mark
    p_mark = sub.add_parser("mark", help="change a sponsor's pipeline status")
    p_mark.add_argument("--name", required=True)
    p_mark.add_argument("--status", required=True, choices=STATUSES)
    p_mark.add_argument("--note")
    p_mark.set_defaults(func=cmd_mark)

    # remove-sponsor
    p_rm = sub.add_parser("remove-sponsor", help="delete a sponsor from the tracker")
    p_rm.add_argument("--name", required=True)
    p_rm.add_argument("--yes", action="store_true")
    p_rm.set_defaults(func=cmd_remove)

    # ui
    p_ui = sub.add_parser("ui", help="launch the local web wizard")
    p_ui.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback)")
    p_ui.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    p_ui.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    p_ui.add_argument("--base-dir", help="directory holding config.yaml and profiles/ (default: project root)")
    p_ui.set_defaults(func=cmd_ui)

    _add_content_parsers(sub)

    # auth
    p_auth = sub.add_parser("auth", help="manage web UI accounts and two-factor enrolment")
    p_auth.add_argument("--base-dir", help="directory holding users.json (default: cwd)")
    auth_sub = p_auth.add_subparsers(dest="auth_command", metavar="SUBCOMMAND")

    p_adduser = auth_sub.add_parser("adduser", help="create an account for the web UI")
    p_adduser.add_argument("--email")
    p_adduser.add_argument("--password", help="avoid: leaves the password in shell history")

    p_auth_list = auth_sub.add_parser("list", help="show accounts and two-factor status")
    p_auth_list.add_argument("--json", action="store_true", help="machine readable output")

    p_passwd = auth_sub.add_parser("passwd", help="change an account password")
    p_passwd.add_argument("--email")

    p_rm_user = auth_sub.add_parser("remove", help="delete an account")
    p_rm_user.add_argument("--email")
    p_rm_user.add_argument("--yes", action="store_true", help="do not prompt")

    p_totp = auth_sub.add_parser("totp", help="show two-factor status for an account")
    p_totp.add_argument("--email")
    p_totp.add_argument("--yes", action="store_true", help="do not prompt")

    p_totp_off = auth_sub.add_parser("totp-disable", help="turn two-factor off for an account")
    p_totp_off.add_argument("--email")
    p_totp_off.add_argument("--yes", action="store_true", help="do not prompt")

    p_recovery = auth_sub.add_parser("recovery", help="reissue single-use recovery codes")
    p_recovery.add_argument("--email")

    p_auth.set_defaults(func=cmd_auth)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE

    log = get_logger("cli")
    try:
        return int(args.func(args))
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_USAGE
    except TrackerError as exc:
        log.error("%s", exc)
        return EXIT_ERROR
    except ContentStoreError as exc:
        log.error("%s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        log.info("Interrupted")
        return EXIT_ERROR
    except SystemExit as exc:  # dependency guard, and CLI-level bail-outs
        return int(exc.code or EXIT_ERROR)
    except Exception as exc:  # noqa: BLE001 - last-resort guard
        log.exception("Unhandled error: %s", exc)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())