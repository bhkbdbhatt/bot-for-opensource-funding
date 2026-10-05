# GenesysPluginSponsorBot

A dependency-light Python tool that promotes one open-source project two ways.

**Pipeline 1 - outreach.** Finds potential sponsors, writes a tailored message
for each one, and delivers it over rate-limited email or forum channels.

**Pipeline 2 - content syndication.** Writes one article and fans it out to
developer communities: DEV (dev.to), Hashnode, Medium, WordPress
(WordPress.com or self-hosted), CoderLegion and DevDojo, plus a generic webhook.
Platforms with a real publishing API are posted automatically; platforms that
publish none get a paste-ready file and wait for you to confirm the live URL.

It is **not** a Genesys plugin runtime. It is a fundraising / developer-relations
tool that promotes one project, and it is deliberately generic - campaigns are
profiles, so it works for projects beyond Genesys.

- Deterministic composers by default; LLM rendering is opt-in and never required.
- Only two third-party dependencies: `pyyaml` and `requests`.
- Rate limits, per-target cooldowns and a failure circuit-breaker on both pipelines.
- Secrets are read from environment variables and never written to disk.
- Every send and every publish is persisted before the next one, so a crash never
  repeats or loses work.
- A quality gate refuses to publish thin, undisclosed or hype-laden content -
  the fastest way to get removed from a developer community.
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

# 4. See the outreach pipeline
python main.py status

# 5. See what content syndication would need
python main.py platforms

# 6. Or use the local web wizard (opens http://127.0.0.1:8765)
python main.py ui
```

`Config.load()` auto-creates `config.yaml` from `config.example.yaml` when it is
missing, so the tool can start without manual copying — but you must edit the
values before anything is real. **Nothing is transmitted until you switch it on:
email/forum need their channel `enabled`, and syndication needs
`content.enabled: true` plus at least one `platforms.<id>.enabled: true`.**

## Commands

### Outreach

```
python main.py discover        # search GitHub topics for new prospects
python main.py add-sponsor     # add a prospect manually
python main.py generate        # preview a message, send nothing
python main.py run             # start the rate-limited scheduler
python main.py status          # both pipelines at a glance
python main.py list            # every sponsor and its status
python main.py mark            # change a sponsor's pipeline status
python main.py remove-sponsor  # delete a sponsor
```

### Content syndication

```
python main.py platforms       # every target: kind, readiness, where to get a key
python main.py draft           # compose an article into content.json
python main.py publish         # send approved articles to their platforms
python main.py content list    # the article ledger and its live URLs
python main.py content show    # read one article + its per-platform shaping
python main.py content add     # add or replace an article by hand
python main.py content approve # human approval - required before publishing
python main.py content confirm # record a live URL after a manual submission
python main.py content reset   # clear a publication so it is offered again
python main.py content mark    # change an article's status
python main.py content remove  # delete an article
```

### Web UI and accounts

```
python main.py auth            # manage web UI accounts and two-factor
python main.py ui              # launch the local web wizard
```

Global flags: `--config PATH`, `--tracker PATH`, `--log-level LEVEL`, `--quiet`.

### Examples

```powershell
# Discover prospects on GitHub and interactively pick which to add
python main.py discover --topics genesys,genesys-cloud --channel email

# Preview the exact email/forum post for one sponsor
python main.py generate --sponsor Acme --channel email

# One scheduler pass that transmits nothing
python main.py run --once --dry-run

# ---- content syndication -----------------------------------------------

# What can this campaign publish, and what is missing?
python main.py platforms
python main.py platforms --probe hashnode     # list your publications

# Compose an article (deterministic composer, nothing transmitted)
python main.py draft --topic "retry policies for contact-centre APIs"

# Or bring your own markdown
python main.py draft --from-file article.md --platform devto,devdojo

# Rehearse: shape, validate and rate-check without transmitting
python main.py publish --once --dry-run

# Go live
python main.py content approve <id> --yes
python main.py publish --once

# A platform with no API needs your confirmation
python main.py content confirm <id> --platform devdojo --url "https://devdojo.com/..."
```

Run `python main.py <command> --help` for the full argument list.

## The content pipeline

```
brief (content.*)  ->  draft  ->  approve  ->  publish per platform  ->  confirm
                      draft       human        live | draft | queued    live URL
```

Five statuses, mirroring the outreach pipeline:

| Status      | Meaning                                                          |
| ----------- | ---------------------------------------------------------------- |
| `draft`     | composed, not yet reviewed                                       |
| `approved`  | a human approved it; waiting for a publish slot                  |
| `queued`    | handed out for manual submission; **no URL is claimed yet**       |
| `published` | live on every target platform; URLs are on record                |
| `failed`    | a publisher reported an error; fix the config and retry          |

One article is a `ContentItem`; each (article, platform) pair is a
`PublishTarget` with its own outcome, URL and retry history. That is why one
post fanned out to six platforms is six independent, separately quota'd,
separately recoverable operations — a failure on CoderLegion never rolls back a
successful WordPress publish.

### Platform support

| Platform     | Integration                                    | Credential                                    |
| ------------ | ---------------------------------------------- | --------------------------------------------- |
| **dev.to**   | Forem v1 REST API, `POST /api/articles`        | `$DEVTO_API_KEY` — [dev.to/settings/extensions](https://dev.to/settings/extensions) |
| **Hashnode** | GraphQL `publishPost` at `gql.hashnode.com`     | `$HASHNODE_PAT` + a `publication_id`; **write mutations need a Pro plan** |
| **Medium**   | REST `POST /v1/users/{id}/posts`                | **legacy** — Medium issues no new integration tokens (since 2025-01-01); pre-2025 tokens still work |
| **WordPress**| `public-api.wordpress.com/rest/v1.1` or `/wp-json/wp/v2` | Application password (`$WP_USERNAME` + `$WP_APP_PASSWORD`) or OAuth (`$WPCOM_OAUTH_TOKEN`) |
| **CoderLegion** | no API — manual submission                   | none; submit at [coderlegion.com/publish-with-us](https://coderlegion.com/publish-with-us) |
| **DevDojo**  | no API — manual submission                     | none; submit at [devdojo.com/community/posts/write](https://devdojo.com/community/posts/write) |
| **webhook**  | one JSON POST to a URL you control             | `$WEBHOOK_URL` plus an optional auth header    |

`python main.py platforms` is the authoritative readiness check: it prints what
each target needs and where to get a credential. It is read-only and safe.

**Why manual mode exists.** CoderLegion and DevDojo publish no API, and inventing
an endpoint would either fail at 3am or — worse — look like it succeeded. So the
bot does the part that can be automated (correct length, tags, project links,
disclosure) and writes `content_outbox/<platform>/<id>.md`. The article stays
`queued` until you run `content confirm` with the real URL. The ledger never
claims a link that does not exist.

**Canonical URLs.** Publish to your own WordPress first, set
`content.canonical_base_url` to the permalink, then syndicate outward. DEV and
Hashnode both use the canonical field to decide a cross-post is not duplicate
content.

### The quality gate

Before any platform sees a word, `content_engine.validate()` refuses the draft if:

- the body is under `content.min_words` or over `content.max_words`;
- it does not link to your project repository or listing;
- it has no author disclosure (`content.disclosure_required`) — undisclosed
  self-promotion is the fastest way to lose a community account;
- it contains banned wording (the built-in hype list plus `content.forbid_words`);
- the title exceeds the platform's word or character limit;
- it has fewer than two `##` sections or no `#` heading.

`draft` and `publish` both report findings; `publishing.enforce_quality_gate:
false` downgrades them to warnings. Read that flag as "I accept that thin or
undisclosed posts may get removed".

### Renderers

The deterministic composer needs no LLM and is the default, in both pipelines.
Set `scheduler.llm_command` (for example `["ollama", "run", "llama3.1"]`) and pass
`--llm` to `draft` / `publish` / `generate` / `run` to render through it. The bot
never calls an LLM API itself. If the renderer errors, returns nothing, or
returns too little to be an article, the deterministic composer takes over.


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

The wizard walks through seven steps:

1. **Project** — describe the plugin you are promoting (name, links, value
   proposition, call to action).
2. **GitHub** — optionally paste a personal access token and set the search
   topics and filters.
3. **Discover** — run a GitHub sweep in the background and watch its live log.
4. **Review** — tick the owners to contact and approve them into the tracker.
5. **Outreach** — set SMTP/forum credentials in memory, preview each message and
   send (dry-run by default).
6. **Content** — five tabs covering the whole syndication pipeline:
   - **Write** — compose an article and see its per-platform gate results;
   - **Brief** — the editorial facts and the publishing behaviour switches;
   - **Platforms** — enable targets, enter credentials in memory, see which are
     ready and where to get a key;
   - **Articles** — the ledger: status, per-platform state, approve, edit, confirm
     a manual URL, retry, delete;
   - **Publish** — the queue of (article, platform) pairs, per-platform quota
     bars, and a dry-run-by-default publish button.
7. **Dashboard** — both pipelines: contact counts, content counts, live URLs,
   per-channel and per-platform daily quota bars, and recent activity.

**Campaigns are isolated profiles.** Each one is a directory under
`profiles/<slug>/` with its own `config.yaml`, `sponsors.json`, `content.json`,
`discovered.json`, log, `forum_outbox/` and `content_outbox/`. The first time you
open the wizard it imports your existing `config.yaml` as a `default` campaign;
you can create more from the **Generic** or **Genesys** preset, so the tool is
reusable for projects other than Genesys.

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

Two pipelines, one shared runtime.

### Outreach

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

### Content syndication

1. **Brief** (`content_engine.py`) — `content.*` plus `plugin.*` become either a
   deterministic markdown article or a prompt for your configured renderer.
2. **Gate** (same module) — length, project link, author disclosure, banned
   wording and per-platform limits are checked before anything is transmitted.
3. **Track** (`content_store.py`) — articles, per-platform publication outcomes,
   live URLs, per-platform daily counters and history in `content.json`, written
   atomically.
4. **Publish** (`publishers/`) — one publisher per platform. API targets POST;
   manual targets write `content_outbox/` and report `queued`.
5. **Schedule** (`promoter.py`) — ticks over (article, platform) pairs, pacing
   requests, honouring quotas and cooldowns, and persisting after every publish.

### Shared

`state.py` owns what both pipelines must get right: atomic JSON writes,
self-healing reads that quarantine a corrupt file, date-keyed counters that roll
over at local midnight, and the cooldown gate.

## Configuration

All settings live in `config.yaml` (see the fully commented
`config.example.yaml`). Secrets are referenced by environment variable name:

| Variable             | Used for                                            |
| -------------------- | --------------------------------------------------- |
| `SMTP_PASSWORD`      | SMTP authentication                                 |
| `GITHUB_TOKEN`       | Optional GitHub PAT, raises rate limits             |
| `FORUM_API_KEY`      | Forum API bearer token (API mode only)              |
| `DEVTO_API_KEY`      | DEV Community (dev.to) Forem API key                |
| `HASHNODE_PAT`       | Hashnode GraphQL Personal Access Token              |
| `MEDIUM_TOKEN`       | Medium integration token (legacy, pre-2025 only)    |
| `WP_USERNAME` / `WP_APP_PASSWORD` | WordPress application-password auth         |
| `WPCOM_OAUTH_TOKEN`  | WordPress.com OAuth2 bearer token                   |
| `WEBHOOK_URL`        | Target URL for the generic webhook publisher        |
| `OUTREACH_AUTH_SECRET` | Master key for web UI password peppering and sealed TOTP secrets |

Key safety settings:

| Setting                                       | Default | Meaning                          |
| --------------------------------------------- | ------- | -------------------------------- |
| `rate_limits.max_emails_per_day`              | 15      | Daily email cap                  |
| `rate_limits.max_forum_posts_per_day`         | 3       | Daily forum cap                  |
| `rate_limits.min_hours_between_attempts`      | 72      | Per-sponsor cooldown             |
| `rate_limits.max_consecutive_failures`        | 5       | Outreach circuit breaker         |
| `rate_limits.max_publishes_per_day`           | 3       | Cross-platform daily publish cap |
| `rate_limits.min_hours_between_platform_posts`| 24      | Per-(article, platform) cooldown |
| `rate_limits.max_content_failures`            | 3       | Publishing circuit breaker       |
| `scheduler.interval_seconds`                  | 7200    | Seconds between outreach ticks   |
| `scheduler.batch_size`                        | 5       | Sponsors handled per tick        |
| `scheduler.dry_run`                           | false   | Generate and log, never transmit |
| `publishing.interval_seconds`                 | 10800   | Seconds between publish ticks    |
| `publishing.batch_size`                       | 3       | (article, platform) pairs per tick |
| `publishing.require_approval`                 | true    | No publish without human approval |
| `publishing.enforce_quality_gate`             | true    | Refuse drafts that fail the gate |
| `publishing.dry_run`                          | false   | Shape and check, never transmit |

### Forum modes

`ForumSender` picks a mode automatically:

- **dry-run** — `--dry-run` is set; nothing is transmitted.
- **api** — `forum.api_url` is set; posts JSON with the configured auth header.
- **manual** (default) — writes the formatted post to `forum_outbox/` and prints
  it with the target thread URL so a human can paste it. The sponsor is still
  marked `contacted` because the post has been prepared and queued.

### Publishing modes

`publishers/` picks a mode per platform, and `python main.py platforms` shows it:

- **dry-run** — shape, validate and report; the ledger is not touched at all, so a
  rehearsal is repeatable and never consumes quota.
- **api** / **webhook** — a documented endpoint; `check_ready()` verifies the
  credential first, so a missing key is a skip rather than an HTTP 401 in the log.
- **manual** — no API exists; write the outbox file and report `queued`.

## Ethics and compliance

- `discovery.py` uses only the public GitHub API and owners' own published
  websites. Contacts are surfaced for a human to confirm before use.
- The prompt rules forbid hype words and fabricated metrics.
- Every email appends the configured `unsubscribe_note` and is marked bulk
  (`X-Priority: 3`).
- Published articles must disclose that you maintain the project, and the gate
  enforces it. Use `content.canonical_base_url` when cross-posting so platforms
  treat the copy as syndication rather than duplicate content.
- Both pipelines are rate-limited independently, both persist after every
  outbound action, and both refuse to run without credentials you supplied.

## Files

| Path                       | Purpose                                     |
| -------------------------- | ------------------------------------------- |
| `main.py`                  | CLI entry point and composition root        |
| `config_loader.py`         | Typed config reader and validation          |
| `models.py`                | Shared dataclasses and constants            |
| `state.py`                 | Atomic JSON, daily counters, cooldown       |
| `tracker.py`               | Outreach ledger (`sponsors.json`)           |
| `content_store.py`         | Syndication ledger (`content.json`)         |
| `platforms.py`             | Registry of syndication targets             |
| `discovery.py`             | GitHub prospecting and email scraping       |
| `prompt_engine.py`         | Outreach message composition                 |
| `content_engine.py`        | Article composition + quality gate          |
| `llm.py`                   | Optional subprocess LLM renderer            |
| `scheduler.py`             | Outreach tick loop, quotas, circuit breaker  |
| `promoter.py`              | Publish tick loop, quotas, circuit breaker   |
| `senders/`                 | Email and forum channel senders             |
| `publishers/`              | One publisher per syndication target        |
| `logging_setup.py`         | Console and rotating-file logging           |
| `webapp/`                  | Local web wizard (stdlib HTTP + SPA)        |
| `webapp/auth.py`           | Password hashing, TOTP, accounts, sessions  |
| `profiles/`                | One isolated campaign directory per slug    |
| `users.json`               | Web UI accounts (git-ignored)               |

## Running tests

There is no automated test suite in the repository proper. Use `--dry-run`,
`--once`, `--max-ticks` and `--json` to exercise both pipelines safely, plus
`_selftest.py` (markdown→HTML, quality gate, content ledger, rate limits, tracker
regressions) and `_webtest.py` (the HTTP security boundary and every content API
route), both of which run against temporary directories and transmit nothing:

```powershell
.\.venv\Scripts\python.exe _selftest.py
.\.venv\Scripts\python.exe _webtest.py
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design.
