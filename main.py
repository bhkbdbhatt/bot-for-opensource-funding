#!/usr/bin/env python3
"""GenesysPluginSponsorBot - command line interface.

    python main.py discover        # scrape GitHub topics for new prospects
    python main.py add-sponsor ... # add a prospect manually
    python main.py generate ...    # dry-run: print the message, send nothing
    python main.py run             # start the rate-limited scheduler
    python main.py status          # pipeline summary
    python main.py list            # every sponsor and status
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
from logging_setup import get_logger, setup_logging
from models import CHANNELS, STATUSES, Sponsor, is_valid_email, now_iso
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
    from senders import get_sender

    config = load_config(args)
    tracker = build_tracker(config, args)
    summary = tracker.summary()
    described = config.describe()

    if args.json:
        emit_json({"config": described, "pipeline": summary})
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Promote an open-source Genesys Cloud plugin to potential sponsors.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py discover --yes --channel email\n"
            "  python main.py add-sponsor --name 'Acme ISV' --email devrel@acme.com\n"
            "  python main.py generate --sponsor Acme --channel forum\n"
            "  python main.py run --once --dry-run\n"
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
    except KeyboardInterrupt:
        log.info("Interrupted")
        return EXIT_ERROR
    except SystemExit as exc:  # dependency guard
        return int(exc.code or EXIT_ERROR)
    except Exception as exc:  # noqa: BLE001 - last-resort guard
        log.exception("Unhandled error: %s", exc)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())