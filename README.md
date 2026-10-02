# GenesysPluginSponsorBot

A dependency-light Python CLI that finds potential sponsors for an open-source
Genesys Cloud plugin, writes a tailored outreach message for each one, and
delivers it over rate-limited email or forum channels.

It is **not** a Genesys plugin runtime. It is a small fundraising / developer
relations tool that promotes one plugin.

- Deterministic message composer by default; LLM rendering is opt-in.
- Only two third-party dependencies: `pyyaml` and `requests`.
- Rate limits, per-recipient cooldowns and a failure circuit-breaker are built in.
- Secrets are read from environment variables and never written to disk.
- Every send is persisted before the next one, so a crash never re-sends.
- Optional zero-dependency local web wizard for the whole workflow
  (`python main.py ui`) — standard library only, no build step.

## Requirements

- Python 3.10+ (developed and tested on 3.12)
- `pip install -r requirements.txt`

## Quick start

```powershell
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your config (config.yaml is git-ignored, an example is provided)
Copy-Item config.example.yaml config.yaml

# 3. Set secrets as environment variables (never in the file)
$env:SMTP_PASSWORD   = "your-smtp-password"
$env:GITHUB_TOKEN    = "ghp_..."      # optional, raises GitHub rate limits

# 4. See the pipeline
python main.py status

# 5. Or use the local web wizard (opens http://127.0.0.1:8765)
python main.py ui
```

`Config.load()` auto-creates `config.yaml` from `config.example.yaml` when it is
missing, so the tool can start without manual copying — but you must edit the
values (`plugin.*`, `email.*`, `forum.*`) before anything is real.

## Commands

```
python main.py discover        # search GitHub topics for new prospects
python main.py add-sponsor     # add a prospect manually
python main.py generate        # preview a message, send nothing
python main.py run             # start the rate-limited scheduler
python main.py status          # pipeline summary
python main.py list            # every sponsor and its status
python main.py mark            # change a sponsor's pipeline status
python main.py remove-sponsor  # delete a sponsor
python main.py auth            # manage web UI accounts and two-factor
python main.py ui              # launch the local web wizard
```

Global flags: `--config PATH`, `--tracker PATH`, `--log-level LEVEL`, `--quiet`.

### Examples

```powershell
# Discover prospects on GitHub and interactively pick which to add
python main.py discover --topics genesys,genesys-cloud --channel email

# Non-interactive discovery, add everything that passes the filters
python main.py discover --yes --limit 20

# Add one prospect by hand
python main.py add-sponsor --name "Acme ISV" --email devrel@acme.com

# Preview the exact email/forum post for one sponsor
python main.py generate --sponsor Acme --channel email

# One scheduler pass that transmits nothing
python main.py run --once --dry-run

# Run forever, ticking every 2 hours
python main.py run
```

Run `python main.py <command> --help` for the full argument list.

## Local web wizard

`python main.py ui` starts a small HTTP server (standard library only) and opens
a single-page wizard for the entire workflow — no build step, no Node, no extra
Python packages.

```powershell
python main.py ui                       # http://127.0.0.1:8765, opens a browser
python main.py ui --port 8899           # different port
python main.py ui --no-browser          # headless / remote shell
python main.py ui --base-dir C:\bots\funding   # where config.yaml and profiles/ live
```

The wizard walks through six steps:

1. **Project** — describe the plugin you are promoting (name, links, value
   proposition, call to action).
2. **GitHub** — optionally paste a personal access token and set the search
   topics and filters.
3. **Discover** — run a GitHub sweep in the background and watch its live log.
4. **Review** — tick the owners to contact and approve them into the tracker.
5. **Outreach** — set SMTP/forum credentials in memory, preview each message and
   send (dry-run by default).
6. **Dashboard** — pipeline counts, per-channel daily quota bars and recent
   activity; change statuses or add contacts by hand.

**Campaigns are isolated profiles.** Each one is a directory under
`profiles/<slug>/` with its own `config.yaml`, `sponsors.json`,
`discovered.json`, log and `forum_outbox/`. The first time you open the wizard it
imports your existing `config.yaml` as a `default` campaign; you can create more
from the **Generic** or **Genesys** preset, so the tool is reusable for projects
other than Genesys.

### Signing in: email, password and two-factor

The wizard requires an account. On first run it asks you to create one in the
browser; afterwards every `/api/*` route needs a signed-in session.

1. Enter your **email address** and a **password** (12+ characters, mixing three
   of lowercase / uppercase / digits / symbols).
2. If two-factor is enrolled, the second screen asks for the **6-digit code** from
   an authenticator app. A single-use recovery code works in its place.

Turn two-factor on from **Security & 2FA** in the sidebar. That screen hands you a
base32 secret and an `otpauth://` URI to add to Google Authenticator, 1Password,
Authy or any TOTP app, then asks you to prove the codes line up. It then shows ten
**recovery codes once** — only their hashes are stored.

The same accounts can be managed from the command line, which is the way to
bootstrap one without a browser:

```powershell
python main.py auth adduser --email you@example.com    # prompts for the password
python main.py auth list                                # accounts + 2FA status
python main.py auth passwd --email you@example.com
python main.py auth recovery --email you@example.com    # reissue recovery codes
python main.py auth totp-disable --email you@example.com
python main.py auth remove --email you@example.com
```

`--base-dir` selects where `users.json` lives (default: the current directory).

**Security model.** The server binds to loopback only and layers four
independent checks: a `Host` allow-list (DNS-rebinding), a per-process
`X-Web-Token` the page reads from a `<meta>` tag (CSRF), path containment for
static files (traversal), and an account with a session cookie on every data
route. The cookie is `HttpOnly; SameSite=Strict` and carries only a random
token; sessions live in memory and are keyed by the token's SHA-256 digest.

Passwords are stored as PBKDF2-HMAC-SHA256 hashes (600k rounds, per-user salt)
peppered with a key derived from `OUTREACH_AUTH_SECRET`. TOTP secrets are
sealed at rest with an encrypt-then-MAC envelope, so `users.json` contains no
usable secret even if it is copied. Five wrong passwords lock an account with
exponential backoff, and a per-client throttle caps login attempts.

**Set `OUTREACH_AUTH_SECRET`** to a long random string before first use:

```powershell
$env:OUTREACH_AUTH_SECRET = python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Without it the server still works, but it generates a key and stores it *inside*
`users.json` — which protects the file no better than the filesystem already
does, and would invalidate every account if you later set the variable. The
server logs a warning at startup when the variable is missing.

Lost your phone? Use a recovery code, then turn two-factor off and enrol again:

```powershell
python main.py auth totp-disable --email you@example.com
```

The wizard drives the same modules as the CLI (`webapp/service.py` is a thin
seam), so both front ends share rate limits, cooldowns and the persist-after-
every-send guarantee.

## How it works

1. **Discover** (`discovery.py`) — queries the public GitHub REST API for repos
   matching configured topics, groups them by owner, filters by stars/activity,
   and optionally scrapes the owner's public website for a contact email.
2. **Track** (`tracker.py`) — keeps prospects, pipeline status, daily send
   counters and an event history in `sponsors.json`, written atomically.
3. **Generate** (`prompt_engine.py`) — composes a channel-specific message from
   templates. With `--llm` it shells out to a command you configure (for example
   `ollama run llama3.1`); the bot itself never calls an LLM API.
4. **Send** (`senders/`) — dispatches through `EmailSender` (SMTP) or
   `ForumSender` (JSON API or a manual copy-paste outbox).
5. **Schedule** (`scheduler.py`) — ticks on an interval, respects daily quotas,
   a per-sponsor cooldown and a consecutive-failure circuit breaker.

## Configuration

All settings live in `config.yaml` (see the fully commented
`config.example.yaml`). Secrets are referenced by environment variable name:

| Variable          | Used for                              |
| ----------------- | ------------------------------------- |
| `SMTP_PASSWORD`   | SMTP authentication                   |
| `GITHUB_TOKEN`    | Optional GitHub PAT, raises rate limits |
| `FORUM_API_KEY`   | Forum API bearer token (API mode only) |
| `OUTREACH_AUTH_SECRET` | Master key for web UI password peppering and sealing TOTP secrets |

Key safety settings:

| Setting                              | Default | Meaning                          |
| ------------------------------------ | ------- | -------------------------------- |
| `rate_limits.max_emails_per_day`     | 15      | Daily email cap                  |
| `rate_limits.max_forum_posts_per_day`| 3       | Daily forum cap                  |
| `rate_limits.min_hours_between_attempts` | 72  | Per-sponsor cooldown             |
| `rate_limits.max_consecutive_failures`   | 5   | Circuit breaker threshold        |
| `scheduler.interval_seconds`         | 7200    | Time between scheduler ticks     |
| `scheduler.batch_size`               | 5       | Sponsors handled per tick        |
| `scheduler.dry_run`                  | false   | Generate and log, never transmit |

### Forum modes

`ForumSender` picks a mode automatically:

- **dry-run** — `--dry-run` is set; nothing is transmitted.
- **api** — `forum.api_url` is set; posts JSON with the configured auth header.
- **manual** (default) — writes the formatted post to `forum_outbox/` and prints
  it with the target thread URL so a human can paste it. The sponsor is still
  marked `contacted` because the post has been prepared and queued.

## Ethics and compliance

- `discovery.py` uses only the public GitHub API and owners' own published
  websites. Contacts are surfaced for a human to confirm before use.
- The prompt rules forbid hype words and fabricated metrics.
- Every email appends the configured `unsubscribe_note` and is marked bulk
  (`X-Priority: 3`).

## Files

| Path                  | Purpose                                  |
| --------------------- | ---------------------------------------- |
| `main.py`             | CLI entry point and composition root     |
| `config_loader.py`    | Typed config reader and validation       |
| `models.py`           | Shared dataclasses and constants         |
| `tracker.py`          | Atomic JSON state and rate-limit ledger  |
| `discovery.py`        | GitHub prospecting and email scraping    |
| `prompt_engine.py`    | Message composition for both channels    |
| `llm.py`              | Optional subprocess LLM renderer         |
| `scheduler.py`        | Tick loop, quotas and circuit breaker    |
| `senders/`            | Channel senders and factory              |
| `logging_setup.py`    | Console and rotating-file logging        |
| `webapp/`             | Local web wizard (stdlib HTTP + SPA)     |
| `webapp/auth.py`      | Password hashing, TOTP, accounts, sessions |
| `profiles/`           | One isolated campaign directory per slug |
| `users.json`          | Web UI accounts (git-ignored)            |

## Running tests

There is currently no automated test suite. Use `--dry-run`, `--once`,
`--max-ticks N` and `--json` to exercise the bot safely. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the design.
