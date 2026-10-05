# Architecture

This document explains the **kind of code** this project is, how the modules fit
together, and the design rules a contributor should follow.

## What kind of codebase this is

A small, flat, single-process Python application. There is no database, no
ORM, no dependency-injection framework and no async runtime. State lives in
JSON files; work happens when you invoke a command or when a tick loop runs.

The one server is optional and local: an outreach wizard served by the standard
library's `http.server` (`webapp/`). It adds no third-party dependency, no build
step and no second implementation of either pipeline — it calls the same modules
the CLI does.

### Two pipelines, one runtime

The tool promotes a project along two independent axes, and treating them as the
same kind of thing is the mistake this architecture exists to prevent:

|                    | Outreach                      | Content syndication        |
| ------------------ | ----------------------------- | -------------------------- |
| Recipient          | a named person                | a platform's audience      |
| Unit of work       | one sponsor, one channel      | one article, many platforms |
| Fan-out            | none — 1:1                    | 1 article → N platforms    |
| Entity             | `Sponsor`                     | `ContentItem` + `Publication` |
| Success value      | `bool`                        | mode, URL, external id     |
| Ledger             | `sponsors.json`               | `content.json`             |
| Budget             | per-channel daily caps        | per-platform **and** global caps |
| Human gate         | scrape review                 | explicit approval + URL confirmation |

So syndication is **not** a third channel. A channel answers "which transport do
I use to reach one person"; a platform answers "where does this article appear,
and what came back?". The `senders/` contract (`send(sponsor, message) -> bool`)
cannot express a fan-out, and a boolean cannot express "published, here is the
URL, on the Pro plan, with these tags trimmed".

What the two *do* share is more than it first appears — atomic writes, corrupt
file recovery, daily counters, cooldown gates, dry-run discipline, an optional
LLM renderer, and a UI seam. That is exactly what `state.py` was extracted for,
so neither pipeline has to re-implement (or forget) a safety property.

### Deliberate style choices

- **Flat module-per-concern layout.** One module owns one job. The only packages
  are `senders/` and `publishers/`, which group interchangeable transports.
- **Functional core, imperative shell.** Pure logic and value objects live in
  `models.py`, `platforms.py`, `content_engine.py` and `prompt_engine.py`; all I/O
  is pushed to the edges (`state.py`, `tracker.py`, `content_store.py`,
  `discovery.py`, `senders/`, `publishers/`).
- **Dataclasses as value objects.** Every persisted or inter-module shape is a
  dataclass with explicit `to_dict` / `from_dict`. No `pickle`, no custom
  serialization framework.
- **Composition root.** `main.py` and `webapp/service.py` are the only places
  that wire `Config` → store → loop → transport. Modules do not construct each
  other.
- **Plugin / strategy pattern.** Delivery channels implement a duck-typed
  `can_send` / `send` contract; publishers implement `check_ready` /
  `can_publish` / `publish` / `describe`. Both are produced by a lazy factory.
- **Registry over hard-coded branches.** `platforms.py` holds one `PlatformSpec`
  per target. Adding a platform means adding a spec and a publisher module —
  the CLI, the config validator, the UI's platform list and the ledger's limits
  all read the registry, so there is no second list to forget.
- **Duck typing at the boundaries.** `discovery.py`, `prompt_engine.py` and
  `content_engine.py` accept an untyped `config` object and duck-type it rather
  than importing `Config`, which breaks an import cycle by convention.
- **Lazy imports.** `main.py`, `senders/__init__.py` and `publishers/__init__.py`
  import optional or heavy modules inside functions so `--help` stays fast and one
  broken platform cannot take down the others.
- **One seam for the UI.** `webapp/service.py` translates UI calls into existing
  modules; `webapp/server.py` owns transport and security only. The UI never
  re-implements discovery, generation, gating, rate limiting or delivery.

The coding style is plain synchronous Python with `from __future__ import
annotations`, builtin generics (`dict[str, Any]`) and PEP 604 unions (`str |
None`). The author lints with Ruff, but no Ruff config file is committed; the
`# noqa: BLE001` / `# noqa: ANN001` markers indicate bugbear and annotation
rulesets are in use. **Check the `F` and `E9` rules specifically** (`ruff check
--select F,E9`) — those catch real defects; the `UP` rules disagree with this
codebase's deliberate `typing.Dict` style, so ignore them.

## Module map

```
main.py            CLI parsing, command dispatch, composition root
  |
  +-- config_loader.py   typed YAML config + env secrets + eager validation
  +-- models.py          Sponsor / DiscoveredSponsor / ContentItem / Publication
  +-- state.py           atomic JSON, daily counters, cooldown, bounded history
  +-- platforms.py       PlatformSpec registry (the single list of targets)
  +-- tracker.py         outreach ledger: sponsors, seen, counters, history
  +-- content_store.py   syndication ledger: items, publications, counters
  +-- scheduler.py       outreach tick loop, quotas, cooldown, circuit breaker
  |     +-- prompt_engine.py   deterministic composer / prompt builder
  |     |     +-- llm.py               optional subprocess renderer
  |     +-- senders/__init__.py        channel factory
  |           +-- senders/email_sender.py   SMTP
  |           +-- senders/forum_sender.py   API or manual outbox
  +-- promoter.py        syndication tick loop (mirrors scheduler.py)
  |     +-- content_engine.py    article composer + quality gate
  |     +-- publishers/__init__.py        publisher factory
  |           +-- publishers/base.py           contract, HTTP, manual hand-off
  |           +-- publishers/devto.py          Forem v1 REST
  |           +-- publishers/hashnode.py       GraphQL
  |           +-- publishers/medium.py         legacy REST (no new tokens)
  |           +-- publishers/wordpress.py      wp.com / wp-json
  |           +-- publishers/manual.py         base for no-API platforms
  |           |     +-- publishers/coderlegion.py
  |           |     +-- publishers/devdojo.py
  |           +-- publishers/webhook.py        generic JSON POST
  |           +-- publishers/markdown_html.py  markdown -> HTML
  +-- discovery.py       GitHub REST prospecting + public email scraping
  +-- logging_setup.py   console + rotating-file logging
  +-- webapp/            optional local web wizard (stdlib http.server)
        +-- auth.py        password hashing, TOTP, accounts, sessions
        +-- server.py      routing, auth/host guards, static serving, run_ui()
        +-- service.py     UI -> existing modules seam (WRITE_LOCK, log capture)
        +-- jobs.py        background job manager with live logs + cancel
        +-- profiles.py    campaign store (profiles/<slug>/config.yaml) + presets
        +-- static/        index.html + app.css + app.js (no build step)
```

## Data flow

```
argparse command
   -> Config.load()          YAML + env secrets + validation
   -> SponsorTracker.load()  atomic JSON state          (outreach)
   -> ContentStore.load()    atomic JSON state          (syndication)
   -> (discover)  GitHubClient.search_repositories()
                     -> aggregate_repositories()
                     -> find_public_email()
                     -> DiscoveredSponsor.to_sponsor()
                     -> tracker.add() / mark_seen()
   -> (run)       Scheduler.tick()
                     -> tracker.next_batch()
                     -> tracker.can_send()          daily cap + cooldown
                     -> sender.can_send()           pre-flight gate
                     -> prompt_engine.generate()    compose or LLM
                      -> sender.send()
                      -> tracker.record_send() + save()   after EVERY send
   -> (draft)     content_engine.generate()       compose or LLM
                     -> content_store.add()               status: draft
   -> (publish)   Promoter.tick()
                     -> content_store.next_targets()     (article, platform) pairs
                     -> content_store.can_publish()      per-platform + global cap,
                                                           cooldown, missing limit
                     -> publisher.can_publish()          check_ready()
                     -> content_engine.prepare_for_platform()
                        clamp title, trim tags, normalise format
                        + content_engine.validate()  <-- HARD GATE
                     -> publisher.publish()          -> PublishOutcome
                      -> content_store.record_publish() + save()  after EVERY publish
                      -> (manual) content_store.confirm()  with the live URL
   -> (ui)        webapp/server.py route -> webapp/service.py
                      -> same module calls as above, on a background job
                      -> GET /api/jobs/<id> streams logs, then the result
```

## Module responsibilities

### `main.py` — composition root and CLI
Defines the argparse tree, the `cmd_*` handlers, console/JSON output helpers and
the exit-code contract (`0` ok, `1` error, `2` usage). `main()` wraps dispatch
with `ConfigError` / `TrackerError` / `ContentStoreError` / `KeyboardInterrupt` /
`SystemExit` / last-resort `Exception` guards. Nothing else constructs the object
graph. Content commands are registered by `_add_content_parsers()` so the
platform id list comes from `models.py` rather than being retyped in help text.

### `config_loader.py` — typed config
`Config` wraps `yaml.safe_load` with coercion helpers (`bool`, `int`, `str`,
`list`, dotted-path access) and runs `_validate()` eagerly. `section()` and
`records()` both accept dotted paths, which is what lets
`rate_limits.platform_daily_limits` be read as a mapping. Missing secrets are
recorded as warnings, not errors. `resolve_secret()` reads a secret from the
environment; the value is never stored on the config object and
`Config.describe()` only ever reports `*_set` booleans.

Validation of the content side is **tolerant by design**: the `content` and
`platforms` sections are optional, only keys actually present are checked, and an
unknown platform id is an error rather than a silent no-op (a typo must not look
like a working configuration that never publishes). `publishing_enabled` is the
single predicate the CLI, the UI and the promoter all ask — it requires both
`content.enabled` and at least one enabled platform.

### `models.py` — the shared vocabulary
Pipeline constants, channel and platform ids, publication modes, validation
helpers, and the four dataclasses every other module agrees on: `Sponsor`,
`DiscoveredSponsor`, `ContentItem` and `Publication`.

Two details are load-bearing:

- **`Publication` is the fan-out ledger.** One entry per (article, platform) pair
  recording mode, status (`pending` / `draft` / `manual` / `live` / `failed`), URL,
  external id and attempts. `is_live` requires both `status == "live"` *and* a
  URL, so a ledger entry can never claim a link that does not exist.
  `last_activity_at` deliberately includes **failed** attempts, because a target
  that answered 401 or 429 has told us it is not ready.
- **`ContentItem.sync_status()` is the only writer of derived status.** Operator
  intent (`draft`, `approved`, `failed`) is never overwritten from below; only
  `published` / `queued` are derived, and a stale `published` demotes to
  `approved` rather than dropping the item out of the queue.

### `state.py` — the shared persistence primitives
`write_json_atomic` / `read_json` / `AtomicJsonStore` (temp file, `fsync`,
`os.replace`, corrupt-file quarantine, schema version), `DailyCounters`
(date-keyed, rolls over at local midnight), `cooldown_reason` (the gate, with the
`attempts` guard that stops a stamped-but-never-sent entry arming itself), and
`BoundedLog` (capped event history). Both ledgers build on it; neither may
re-implement a write.

### `platforms.py` — the registry
One frozen `PlatformSpec` per target, holding the immutable facts a publisher
needs: `kind` (`api` / `manual` / `webhook`), `content_format` (`markdown` /
`html`), auth shape, `token_env`, `tag_limit`, `max_title_words`,
`max_body_bytes`, `supports_canonical`, `supports_draft`, `legacy`, plus the docs
URL, the credential help URL and the submission URL. `legacy` exists because
Medium genuinely stopped issuing integration tokens — the target stays functional
and documented, but ships disabled and warns when enabled.

`models.PLATFORM_IDS` is the canonical tuple and `platforms.py` imports it, so
there is exactly one list to keep in sync and no import cycle.

### `tracker.py` — outreach state and rate limiting
A `SponsorTracker` over `sponsors.json`: prospects keyed by normalized name, a
discovery-dedupe `seen` map, an event `history` and per-day send counters. Two
robustness properties are core to the design, and both now live in `state.py`:

- **Atomic writes.** A temp file in the same directory, flushed, `fsync`ed, then
  `os.replace()`d into place. A crash mid-write cannot corrupt state.
- **Self-healing.** Corrupt JSON is moved aside to `<name>.corrupt` and a fresh
  state is started; a migration blanks stale `last_contacted_at` values so the
  cooldown cannot arm itself against a never-sent sponsor.

`add()` only fills empty fields on existing entries, so re-running discovery can
never clobber pipeline state. Its public surface is unchanged by the `state.py`
refactor; only its internals moved.

### `content_store.py` — syndication state and rate limiting
A `ContentStore` over `content.json` with the same durability, plus three things
outreach does not need: the fan-out ledger, a status machine with an explicit
human step, and **two** rate-limit dimensions (a per-platform cap and a global
`all` cap, so one article cannot be sprayed across six platforms in a minute).

Four methods encode the policy decisions:

- `can_publish()` — a platform with **no configured limit is refused**, not
  treated as unlimited. A missing `platform_daily_limits` key must never become an
  unbounded firehose.
- `record_publish()` — distinguishes `live` (public, records the URL), `draft`
  (accepted but not public — DEV `published: false`, WordPress `status: draft`)
  and `manual` (handed to a human). Quota is consumed for any non-dry-run attempt,
  successful or not.
- `record_blocked()` — a quality-gate refusal is an editorial state, not a
  delivery event: no attempt, no quota, no cooldown. Otherwise a thin article you
  have not finished writing would quietly burn a platform's daily budget.
- `confirm()` / `unconfirm()` — the only route from `queued` to `published`, and it
  demands an `http(s)` URL. `unconfirm()` restores the operator's approval and
  clears the cooldown, because "send that one again" is a deliberate override.

`add()` refreshes an article's body and merges its platform list without ever
losing publication history, so re-running `draft` is safe.

### `prompt_engine.py` / `content_engine.py` — two media, one discipline
`prompt_engine` composes per-channel outreach messages (email: 200 words,
7-part structure; forum: 300 words, community tone, markdown) plus a hard-rules
list forbidding hype words and fabricated metrics.

`content_engine` composes per-platform articles and, more importantly, **gates**
them. `validate()` enforces a word budget, the presence of a project link, an
author disclosure, the banned-wording list (built-in hype words plus
`content.forbid_words`), title limits and heading structure.
`prepare_for_platform()` is the last step before a publisher runs — it clamps the
title, trims tags to the platform's limit, normalises the body to the platform's
format and re-runs the gate against the *final* payload.

Both engines share the same fail-safe contract: deterministic output by default,
an optional `llm_renderer` (`llm.SubprocessLLM`), and a fallback to the
deterministic composer on error, empty output, or output too short to be an
article. `content_engine.compose()` does **not** pad itself to reach `min_words`;
if your config is too thin the gate says so, and the fix is more config or a real
renderer.

### `llm.py` — optional external renderer
A subprocess only: prompt on stdin, message on stdout. The bot never calls an LLM
API itself. All failure modes (`TimeoutExpired`, `OSError`, non-zero exit,
too-short output) surface as `LLMError`, which both engines treat as a cue to fall
back.

### `scheduler.py` / `promoter.py` — the two tick loops
Structurally identical on purpose: pick a batch, check the circuit breaker,
channel availability, the store's daily caps and cooldown, the pre-flight gate,
generate, send, then record and **persist immediately**. Per-item failures never
abort the tick; `max_consecutive_failures` halts the loop instead.
`run_forever()` is a cron-like loop with an interruptible sleep and SIGINT/SIGTERM
handling that always restores the previous handlers.

Four deliberate divergences in `Promoter`:

1. **A batch is pairs, not sponsors** — each pair gets its own URL, history and
   quota, so a failure on one platform cannot roll back another.
2. **The quality gate is a hard precondition.** `Scheduler` *warns* about a word
   budget; the promoter *refuses* to publish, because a rule-breaking post gets
   removed and the removal is public. `publishing.enforce_quality_gate: false`
   downgrades it to a warning.
3. **Manual targets are a success state, not a fallback** — `queued` counts as a
   successful tick.
4. **A dry run writes nothing at all.** It does not consume quota, record an
   attempt, or arm a cooldown, so a rehearsal is repeatable and the first real run
   after it is not skipped. (`Scheduler` *does* consume quota on a dry run — a
   wart of the older pipeline, deliberately not copied.)

`Promoter` also paces outbound requests (`publishing.request_delay_seconds`) so a
batch never looks like a scraper.

### `senders/` and `publishers/` — the two transport packages
Both are lazily-imported factories over duck-typed transports, but their contracts
differ on purpose:

|               | `senders/`                          | `publishers/`                            |
| ------------- | ----------------------------------- | ---------------------------------------- |
| Gate          | `can_send(sponsor)`                 | `check_ready()` + `can_publish(item)`    |
| Action        | `send(sponsor, message) -> bool`    | `publish(item, payload) -> PublishOutcome` |
| Result        | boolean                             | mode, URL, external id, instructions, outbox path |
| Input shaping | the sender parses `Title:` itself   | `content_engine` shapes it first         |

`senders/`:

- `email_sender.py` — stdlib `smtplib` + `EmailMessage`, STARTTLS or implicit
  TLS, HTML alternative rendering, bulk-friendly headers.
- `forum_sender.py` — three-mode fallback: dry-run, JSON API, or manual outbox.
  Manual mode never transmits; it writes a markdown copy to `forum_outbox/`.

`publishers/base.py` owns everything shared: `PublishOutcome`, the config
accessors, the credential indirection (`token_envs` — a list, because WordPress
accepts either an application password or an OAuth token), the mode determination,
`_request()` (timeout, transport-error mapping, a single retry for 429/5xx
honouring `Retry-After`, never logging a credential), the manual hand-off
(`content_outbox/<platform>/<id>.md` plus printed steps), and `check()`.

`check()` reports real readiness **ignoring dry-run** — a rehearsal that reports
"ready" when the credential is missing defeats the only purpose of a readiness
check.

`publishers/markdown_html.py` is the reason WordPress and Medium work without a
new dependency. It implements the markdown subset the composer produces plus the
common human constructs, escapes everything before adding markup, and filters URL
schemes to `http(s)` / `mailto` / relative — so no input can inject markup or a
`javascript:` / `data:` URL. Constructs outside the subset degrade to escaped text
rather than being dropped.

### `discovery.py` — GitHub prospecting
`GitHubClient` wraps a `requests.Session` for the search, user and rate-limit
endpoints and maps HTTP failures to actionable `DiscoveryError` messages. Owner
lookups are budgeted (`max_owner_lookups`) and results are deduplicated across
topics. Email scraping is best-effort over only public profile/website data,
filters noise domains and prefers role addresses.

### `logging_setup.py` — logging
Console (ANSI, TTY-aware) plus a rotating file handler. `setup_logging()` makes
itself idempotent by clearing existing root handlers and silences `urllib3` to
WARNING. `--quiet` suppresses only the console, never the log file.

### `webapp/` — optional local wizard
A zero-dependency front end over the same modules. It is deliberately split so no
layer duplicates pipeline logic:

- `server.py` — a `ThreadingHTTPServer` with a regex router (`ROUTES`). It owns
  the security boundary: loopback-only binding, a `Host` allow-list against
  DNS-rebinding, a per-process `X-Web-Token` checked with `hmac.compare_digest`,
  an account session on every data route, path containment for static files, a
  strict CSP, and a `__WEB_TOKEN__` placeholder substituted into `index.html` at
  serve time. Two route tables: `PUBLIC_ROUTES` holds only what signing in needs;
  everything else in `ROUTES` runs after `_session()` resolves a live session.

  The exception ladder is part of this boundary, not an afterthought — it is what
  keeps bad user input from surfacing as an unhandled 500: `AuthError` → 400,
  `ApiError` → its own status, `ConfigError` / `TrackerError` /
  `ContentStoreError` → 400, `ContentNotFound` → 404, `ValueError` → 400 (the
  service layer's input-validation channel), everything else → 500.

  Content routes put the item id in the path (`/content/items/<id>`) so the URL
  alone identifies the resource.
- `auth.py` — accounts, password hashing, TOTP and sessions. Standard library
  only, so the whole scheme is auditable in one file (see below).
- `service.py` — the seam. `build_tracker`, `build_renderer`, `run_discovery`,
  `approve_candidates`, `send_sponsors`, `set_secrets`, `draft_article`,
  `publish_content`, `confirm_article` etc. wrap existing modules. `WRITE_LOCK`
  (an `RLock`) serializes read-modify-write cycles on a profile's tracker *and*
  its content ledger so concurrent jobs cannot clobber each other, and
  `capture_logs()` mirrors application log records into the running job. Content
  functions deliberately let `ContentStoreError` / `ContentNotFound` propagate
  rather than erasing the type, because the server maps them to 400/404.
- `jobs.py` — `JobManager` runs long operations (discovery, delivery, publishing)
  on daemon threads. A `JobContext` gives the body `log`, `progress` and
  cooperative `check_cancelled`; the browser polls `GET /api/jobs/<id>?since=N`
  for the incremental log and final result. History is bounded (`MAX_JOBS`,
  `MAX_LOG_LINES`).
- `profiles.py` — `ProfileStore` treats each campaign as a directory
  `profiles/<slug>/config.yaml`. Because a profile's `base_dir` is its own
  directory, all relative paths (`sponsors.json`, `content.json`,
  `discovered.json`, `forum_outbox/`, `content_outbox/`) fall inside it, giving
  isolation for free. `PRESETS` supplies the `generic` and `genesys` templates —
  both include the `content`, `platforms` and `publishing` blocks — and
  `ensure_default()` imports the project's `config.yaml` on first run. Writes are
  atomic. `CONTENT_BINDINGS` and `platform_bindings()` expose the registry to the
  wizard so the UI is driven by data, not by a hard-coded field list.
- `static/` — a vanilla-JS single page (`app.js`) with no bundler; `fetch` calls
  carry the `X-Web-Token` header and `credentials: 'same-origin'`. Long operations
  never block a request; the page polls the job endpoints instead. `renderAuth()`
  swaps between the sign-in screens and the wizard, so the application markup is
  never the thing that decides who is signed in — the API is.

### `webapp/auth.py` — email, password and two-factor
Sign-in is a two-step exchange. `POST /api/auth/login` checks the password and, if
two-factor is enrolled, returns a short-lived `challenge` instead of a session;
`POST /api/auth/totp` exchanges that challenge for the code. The challenge is
bound to the address and expires in five minutes, and is deliberately *not*
consumed on a mistyped code — brute force is bounded by that lifetime,
`LoginThrottle` and the lockout instead, so a typo does not force the password to
be retyped.

Storage and cryptography, deliberately dependency-free:

| Concern            | Choice                                                                   |
| ------------------ | ------------------------------------------------------------------------ |
| Password           | PBKDF2-HMAC-SHA256, 600k rounds, 16-byte random salt, peppered            |
| TOTP               | RFC 6238, SHA-1, 6 digits, 30s step, +/-1 step of drift                  |
| TOTP at rest       | HMAC-SHA256 counter-mode keystream, then HMAC authenticated envelope      |
| Recovery codes     | 10 single-use codes, stored hashed, shown once                           |
| Session            | In-memory, random cookie token keyed by its SHA-256 digest                |
| Cookie             | `HttpOnly; SameSite=Strict; Path=/`                                       |
| Brute force        | Lockout after 5 failures with exponential backoff + per-client throttle   |

`seal()`/`unseal()` are encrypt-then-MAC: the envelope's MAC covers the version,
the user id and the ciphertext, so editing `users.json` to swap in an attacker's
TOTP secret fails the integrity check rather than silently authenticating. The
master key comes from `OUTREACH_AUTH_SECRET`; when that is unset a random key is
generated once and kept *inside* the file, which is logged as a warning because
it protects the file no better than the filesystem already does.

`UserStore` writes atomically exactly like the other stores, and `SessionStore` /
`LoginThrottle` are guarded by locks because `ThreadingHTTPServer` handles each
request on its own thread.

The UI is a **thin client of both pipelines**: it cannot publish an article the
CLI would refuse, and it inherits the persist-after-every-action guarantee because
delivery runs through `Promoter.publish_targets()`.

## State model

`sponsors.json` is the outreach source of truth:

```json
{
  "version": 1,
  "sponsors": [ { "name": "...", "channel": "email", "status": "new", "...": "..." } ],
  "seen":     { "<repo>:<owner>": { "first_seen_at": "...", "sponsor_status": "new" } },
  "rate_counters": { "date": "YYYY-MM-DD", "email": 0, "forum": 0 },
  "history":  [ { "at": "...", "event": "send", "sponsor": "...", "ok": true } ]
}
```

`content.json` is the syndication source of truth:

```json
{
  "version": 1,
  "items": [
    {
      "id": "retry-policies",
      "title": "Retry policies for contact-centre APIs",
      "body_markdown": "# ...",
      "status": "approved",
      "platforms": ["devto", "devdojo"],
      "tags": ["genesys"],
      "publications": {
        "devto":  { "mode": "api", "status": "live", "url": "https://dev.to/...",
                    "attempts": 1, "published_at": "...", "last_attempt_at": "..." },
        "devdojo": { "mode": "manual", "status": "manual", "attempts": 1,
                    "queued_at": "...", "url": "" }
      }
    }
  ],
  "rate_counters": { "date": "YYYY-MM-DD", "devto": 1, "devdojo": 1, "all": 2 },
  "history":  [ { "at": "...", "event": "publish", "platform": "devto", "ok": true } ]
}
```

Note `"all"` in the counters: the global cap is a real target, not a computed sum,
so it is rate-limited like everything else.

## Cross-cutting invariants

These rules are what keep the bot safe to run unattended. Preserve them when
changing code:

1. **Secrets never touch disk.** Config stores only env var *names*. Passwords are
   stored only as peppered PBKDF2 hashes; TOTP secrets only inside an
   authenticated envelope. The UI writes credentials into `os.environ` only.
2. **Persist before proceeding.** Both tick loops save after every outbound action,
   so a crash cannot re-contact a person or re-publish an article.
3. **Atomic state writes.** Never write JSON in place; use
   `state.write_json_atomic`.
4. **A dry run has no side effects.** Publishing never touches quota, attempts or
   cooldowns in dry-run mode.
5. **Fail soft per item.** Individual errors are recorded, not raised, so one bad
   address or platform cannot stop a batch.
6. **Budget every outbound request.** GitHub calls are paced and owner lookups are
   capped; emails, forum posts, API publishes and webhook calls are quota- and
   cooldown-limited.
7. **Human confirmation, twice over.** Scraped contacts are surfaced for review and
   never auto-sent; articles require explicit approval, and a platform with no API
   stays `queued` until a human supplies a live URL.
8. **Never claim a URL that does not exist.** `Publication.is_live` requires a URL;
   `confirm()` rejects anything that is not `http(s)`.
9. **No mandatory LLM dependency.** Deterministic composers are the default and the
   fallback, in both pipelines.
10. **A missing limit is a refusal, not a permission.** Unconfigured channels and
    platforms get no capacity.
11. **The web UI stays local and gated.** Bind to loopback, enforce the `Host`
    allow-list and the per-process `X-Web-Token`, require a signed-in session on
    every data route, and put UI-entered secrets in the process environment only.
12. **One implementation of each pipeline.** The UI calls the same modules the CLI
    does; never duplicate discovery, generation, gating, rate limiting or delivery.
13. **Authorisation is decided server-side.** The SPA hides the wizard before
    sign-in, but only the API decides what a request may see.

## Extension points

- **New delivery channel:** add `senders/<name>_sender.py`, implement
  `can_send` / `send`, register it in `senders/__init__.py`, add a `CHANNEL_*`
  constant in `models.py`.
- **New syndication platform:** add `publishers/<id>.py` with a `BasePublisher`
  subclass exposing `build(config, dry_run=...)`, add a `PlatformSpec` to
  `platforms.py`, and list the id in `models.PLATFORM_IDS`. The factory, the config
  validator, `platforms` output, the UI's platform list, the ledger's default daily
  limits and the CLI's `--platform` choices all follow.
- **A manual-submission platform:** subclass `publishers.manual.ManualPublisher`
  and override `submit_url` / `categories` / `submission_instructions` — as
  `coderlegion.py` and `devdojo.py` do in a dozen lines each.
- **New prospect source:** add a client alongside `discovery.py` that returns
  `DiscoveredSponsor` values and feeds `tracker.add()`.
- **New rendering backend:** supply any `Callable[[str], str]` as `llm_renderer`
  to `prompt_engine.generate()` or `content_engine.generate()`.
- **A new gate rule:** add it to `content_engine.validate()`. It then runs in
  `draft` (reporting), `publish` (refusing) and the UI (per platform), so one
  edit covers every front end.
- **New campaign preset:** add a `factory` entry to `PRESETS` in
  `webapp/profiles.py`; it appears in the wizard's preset list automatically and
  keeps the tool reusable for projects beyond Genesys.

## Known gaps

- No automated test suite in the repository proper. `_selftest.py` and
  `_webtest.py` exist and run green against temp directories, but they are ad-hoc
  harnesses rather than a `pytest` suite wired into CI, and they transmit nothing.
- No packaging (`pyproject.toml`), Dockerfile or CI.
- The deterministic composer produces a competent ~600-word article, not a
  memorable one. It is a starting point; `--llm` or a hand-written `--from-file`
  article is the real answer.
- `publishers/markdown_html.py` implements the markdown subset the composer emits
  plus common constructs. Reference links, raw-HTML passthrough, lists nested
  deeper than one level and setext headings are not implemented; they render as
  escaped text. Raw-HTML passthrough was left out on purpose — in a tool that runs
  unattended it is a stored-XSS vector.
- `HashnodePublisher` write mutations are gated on Hashnode Pro. There is no
  non-Pro path; the error message says so.
- Medium is a legacy integration: the code is correct for a pre-2025 token, but
  new accounts cannot obtain one.
- `scheduler.bootstrap()` and `senders.preflight()` are public helpers not used by
  `main.py`; `SchedulerHalted` is defined but never raised (the boolean `halted`
  flag is the real mechanism).
- Every account is an administrator. There is no second factor *for* the CSRF
  token and no per-profile permission, so anyone who can sign in can send on every
  campaign.
- Sessions are in-memory, so a restart signs everyone out, and the cookie has no
  `Secure` attribute because the server speaks plain HTTP on loopback. Do not bind
  the wizard to a public interface.
- `main()` maps `SystemExit(0)` to exit code 1 (`exc.code or EXIT_ERROR`), so
  helpers must return `EXIT_OK` rather than raise it.
- One configured webhook URL per campaign, with no per-request signing. Use it
  against an endpoint you trust with your content.
