# Architecture

This document explains the **kind of code** this project is, how the modules fit
together, and the design rules a contributor should follow.

## What kind of codebase this is

A small, flat, single-process Python CLI application. There is no database, no
ORM, no dependency-injection framework and no async runtime. State lives in a
single JSON file; work happens when you invoke a command or when the scheduler's
tick loop runs.

The one server is optional and local: an outreach wizard served by the standard
library's `http.server` (`webapp/`). It adds no third-party dependency, no build
step and no second implementation of the pipeline — it calls the same modules the
CLI does.

The deliberate style choices are:

- **Flat module-per-concern layout.** One module owns one job. The only package
  is `senders/`, which groups interchangeable delivery channels.
- **Functional core, imperative shell.** Pure logic and value objects live in
  `models.py` and `prompt_engine.py`; all I/O is pushed to the edges
  (`tracker.py`, `discovery.py`, `senders/`).
- **Dataclasses as value objects.** Every persisted or inter-module shape is a
  dataclass with explicit `to_dict` / `from_dict`. No `pickle`, no custom
  serialization framework.
- **Composition root.** `main.py` is the only place that wires `Config` →
  `SponsorTracker` → `Scheduler` → senders. Modules do not construct each other.
- **Plugin / strategy pattern.** Delivery channels implement a duck-typed
  `can_send` / `send` contract and are produced by a factory.
- **Duck typing at the boundaries.** `discovery.py` and `prompt_engine.py`
  accept an untyped `config` object and duck-type it rather than importing
  `Config`, which breaks an import cycle by convention.
- **Lazy imports.** `main.py` and `senders/__init__.py` import optional or heavy
  modules inside functions so `--help` stays fast and one broken optional
  dependency cannot take down an unrelated channel.
- **One seam for the UI.** `webapp/service.py` translates UI calls into existing
  modules; `webapp/server.py` owns transport and security only. The UI never
  re-implements discovery, generation, rate limiting or delivery.

The coding style is plain synchronous Python with `from __future__ import
annotations`, builtin generics (`dict[str, Any]`) and PEP 604 unions (`str |
None`). The author lints with Ruff, but no Ruff config file is committed; the
`# noqa: BLE001` / `# noqa: ANN001` markers indicate bugbear and annotation
rulesets are in use.

## Module map

```
main.py            CLI parsing, command dispatch, composition root
  |
  +-- config_loader.py   typed YAML config + env secrets + eager validation
  +-- models.py          Sponsor / DiscoveredSponsor dataclasses + constants
  +-- tracker.py         atomic JSON store: sponsors, seen, counters, history
  +-- scheduler.py       tick loop, quotas, cooldown, circuit breaker
  |     +-- prompt_engine.py   deterministic composer / prompt builder
  |     |     +-- llm.py               optional subprocess renderer
  |     +-- senders/__init__.py        channel factory
  |           +-- senders/email_sender.py   SMTP
  |           +-- senders/forum_sender.py   API or manual outbox
  +-- discovery.py       GitHub REST prospecting + public email scraping
  +-- logging_setup.py   console + rotating-file logging
  +-- webapp/            optional local web wizard (stdlib http.server)
  |     +-- auth.py        password hashing, TOTP, accounts, sessions
  |     +-- server.py      HTTP transport, routing and the security boundary
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
   -> SponsorTracker.load()  atomic JSON state
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
   -> (ui)        webapp/server.py route -> webapp/service.py
                      -> same module calls as above, on a background job
                      -> GET /api/jobs/<id> streams logs, then the result
```

## Module responsibilities

### `main.py` — composition root and CLI
Defines the argparse tree, the nine `cmd_*` handlers, console/JSON output
helpers and the exit-code contract (`0` ok, `1` error, `2` usage). `main()` wraps
dispatch with `ConfigError` / `TrackerError` / `KeyboardInterrupt` /
`SystemExit` / last-resort `Exception` guards. Nothing else constructs the
object graph.

### `config_loader.py` — typed config
`Config` wraps `yaml.safe_load` with coercion helpers (`bool`, `int`, `str`,
`list`, dotted-path access) and runs `_validate()` eagerly. Missing secrets are
recorded as warnings, not errors. `resolve_secret()` reads a secret from the
environment; the value is never stored on the config object and
`Config.describe()` only ever reports `*_set` booleans.

### `models.py` — the shared vocabulary
Pipeline constants (`new`, `contacted`, `replied`, `sponsored`, `failed`),
channel constants, validation helpers and the two dataclasses every other module
agrees on: `Sponsor` (a tracked prospect) and `DiscoveredSponsor` (a raw GitHub
hit, which projects onto a `Sponsor` via `to_sponsor()`). Ranking is
`(-priority, -stars, added_at, name)`.

### `tracker.py` — state and rate limiting
A JSON-backed store keyed by normalized name. It holds sponsors, a
discovery-dedupe `seen` map, an event `history` and per-day send counters. Two
robustness properties are core to the design:

- **Atomic writes.** `save()` writes a temp file in the same directory, flushes
  and `fsync`s it, then `os.replace()`s it into place. A crash mid-write cannot
  corrupt state.
- **Self-healing.** Corrupt JSON is moved aside to `<name>.corrupt` and a fresh
  state is started; a migration blanks stale `last_contacted_at` values so the
  cooldown cannot arm itself against a never-sent sponsor.

`add()` only fills empty fields on existing entries, so re-running discovery can
never clobber pipeline state.

### `scheduler.py` — the tick loop
One tick picks a batch, checks the circuit breaker, channel availability, the
tracker's daily cap and cooldown, the sender's pre-flight gate, generates the
message, sends inside a broad `try/except`, then records and **persists the send
immediately**. Per-sponsor failures never abort the tick; `max_consecutive_failures`
halts the loop instead. `run_forever()` is a cron-like loop with an interruptible
sleep and SIGINT/SIGTERM handling that always restores the previous handlers.

### `prompt_engine.py` — one template, two channels
Holds per-channel rules (email: 200 words, 7-part structure; forum: 300 words,
community tone, markdown) plus a hard-rules list that forbids hype words and
fabricated metrics. `generate()` uses the deterministic composer when no
renderer is supplied, and falls back to it if an LLM renderer raises, returns
nothing or produces too little output. The word limit is enforced at the end.

### `llm.py` — optional external renderer
A subprocess only: prompt on stdin, message on stdout. The bot never calls an
LLM API itself. All failure modes (`TimeoutExpired`, `OSError`, non-zero exit,
too-short output) surface as `LLMError`, which the prompt engine treats as a
cue to fall back.

### `discovery.py` — GitHub prospecting
`GitHubClient` wraps a `requests.Session` for the search, user and rate-limit
endpoints and maps HTTP failures to actionable `DiscoveryError` messages. Owner
lookups are budgeted (`max_owner_lookups`) and results are deduplicated across
topics. Email scraping is best-effort over only public profile/website data,
filters noise domains and prefers role addresses.

### `senders/` — interchangeable channels
`get_sender()` lazily imports and returns a channel. The contract is two
methods: `can_send(sponsor) -> (bool, str)` and `send(sponsor, message) -> bool`.
Adding a channel means adding a module and a branch in the factory.

- `email_sender.py` — stdlib `smtplib` + `EmailMessage`, STARTTLS or implicit
  TLS, HTML alternative rendering, bulk-friendly headers.
- `forum_sender.py` — three-mode fallback: dry-run, JSON API, or manual outbox.
  Manual mode never transmits; it writes a markdown copy to `forum_outbox/`.

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
  an account session on every data route, path-containment for static files, a
  strict CSP, and a `__WEB_TOKEN__` placeholder substituted into `index.html` at
  serve time. Two route tables: `PUBLIC_ROUTES` holds only what signing in needs;
  everything else in `ROUTES` runs after `_session()` resolves a live session.
- `auth.py` — accounts, password hashing, TOTP and sessions. Standard library
  only, so the whole scheme is auditable in one file (see below).
- `service.py` — the seam. `build_tracker`, `build_renderer`, `run_discovery`,
  `approve_candidates`, `send_sponsors`, `set_secrets`, etc. wrap existing
  modules. `WRITE_LOCK` (an `RLock`) serializes read-modify-write cycles on a
  profile's tracker so concurrent jobs cannot clobber each other, and
  `capture_logs()` mirrors application log records into the running job.
- `jobs.py` — `JobManager` runs long operations (discovery, delivery) on daemon
  threads. A `JobContext` gives the body `log`, `progress` and cooperative
  `check_cancelled`; the browser polls `GET /api/jobs/<id>?since=N` for the
  incremental log and final result. History is bounded (`MAX_JOBS`,
  `MAX_LOG_LINES`).
- `profiles.py` — `ProfileStore` treats each campaign as a directory
  `profiles/<slug>/config.yaml`. Because a profile's `base_dir` is its own
  directory, all relative paths (`sponsors.json`, `discovered.json`,
  `forum_outbox/`) fall inside it, giving isolation for free. `PRESETS` supplies
  the `generic` and `genesys` templates and `ensure_default()` imports the
  project's `config.yaml` on first run. Writes are atomic.
- `static/` — a vanilla-JS single page (`app.js`) with no bundler; `fetch` calls
  carry the `X-Web-Token` header and `credentials: 'same-origin'`. Long
  operations never block a request; the page polls the job endpoints instead.
  `renderAuth()` swaps between the sign-in screens and the wizard, so the
  application markup is never the thing that decides who is signed in — the API
  is.

### `webapp/auth.py` — email, password and two-factor

Sign-in is a two-step exchange. `POST /api/auth/login` checks the password and,
if two-factor is enrolled, returns a short-lived `challenge` instead of a
session; `POST /api/auth/totp` exchanges that challenge for the code. The
challenge is bound to the address and expires in five minutes, and is
deliberately *not* consumed on a mistyped code — brute force is bounded by that
lifetime, `LoginThrottle` and the lockout instead, so a typo does not force the
password to be retyped.

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

`UserStore` writes atomically (temp file, `fsync`, `os.replace`) exactly like
`tracker.py`, and `SessionStore` / `LoginThrottle` are guarded by locks because
`ThreadingHTTPServer` handles each request on its own thread.

The UI is a **thin client of the pipeline**: it cannot send to someone the
tracker would refuse, and it inherits the persist-after-every-send guarantee
because delivery runs through `Scheduler.deliver()`.

## State model

`sponsors.json` is the single source of truth and carries its own schema
version:

```json
{
  "version": 1,
  "sponsors": [ { "name": "...", "channel": "email", "status": "new", "...": "..." } ],
  "seen":     { "<repo>:<owner>": { "first_seen_at": "...", "sponsor_status": "new" } },
  "rate_counters": { "date": "YYYY-MM-DD", "email": 0, "forum": 0 },
  "history":  [ { "at": "...", "action": "send", "details": {} } ]
}
```

## Cross-cutting invariants

These rules are what keep the bot safe to run unattended. Preserve them when
changing code:

1. **Secrets never touch disk.** Config stores only env var *names*. Passwords are
   stored only as peppered PBKDF2 hashes; TOTP secrets only inside an
   authenticated envelope.
2. **Persist before proceeding.** `Scheduler.tick()` saves after every send so a
   crash cannot re-contact the same person.
3. **Atomic state writes.** Never write JSON in place; use the temp-file and
   replace pattern.
4. **Fail soft per sponsor.** Individual errors are recorded, not raised, so one
   bad address cannot stop a batch.
5. **Budget every outbound request.** GitHub calls are paced and owner lookups
   are capped; email and forum sends are quota- and cooldown-limited.
6. **Human confirmation for scraped contacts.** Discovery surfaces addresses for
   review; it does not auto-send.
7. **No mandatory LLM dependency.** Deterministic composers are the default and
   the fallback.
8. **The web UI stays local and gated.** Bind to loopback, enforce the `Host`
   allow-list and the per-process `X-Web-Token`, require a signed-in session on
   every data route, and put UI-entered secrets in the process environment
   only — never on disk.
9. **One implementation of the pipeline.** The UI calls the same modules the CLI
   does; never duplicate discovery, generation, rate-limiting or delivery.
10. **Authorisation is decided server-side.** The SPA hides the wizard before
    sign-in, but only the API decides what a request may see.

## Extension points

- **New channel:** add `senders/<name>_sender.py`, implement `can_send` / `send`,
  register it in `senders/__init__.py`, add a `CHANNEL_*` constant in
  `models.py`.
- **New prospect source:** add a client alongside `discovery.py` that returns
  `DiscoveredSponsor` values and feeds `tracker.add()`.
- **New rendering backend:** supply any
  `Callable[[str], str]` as `llm_renderer` to `prompt_engine.generate()`.
- **New campaign preset:** add a `factory` entry to `PRESETS` in
  `webapp/profiles.py`; it appears in the wizard's preset list automatically and
  keeps the tool reusable for projects beyond Genesys.

## Known gaps

- No automated tests (use `--dry-run`, `--once`, `--max-ticks`, `--json`).
- No packaging (`pyproject.toml`), Dockerfile or CI.
- `scheduler.bootstrap()` and `senders.preflight()` are public helpers not used
  by `main.py`; `SchedulerHalted` is defined but never raised (the boolean
  `halted` flag is the real mechanism).
- Every account is an administrator. There is no second factor *for* the CSRF
  token and no per-profile permission, so anyone who can sign in can send on
  every campaign.
- Sessions are in-memory, so a restart signs everyone out, and the cookie has no
  `Secure` attribute because the server speaks plain HTTP on loopback. Do not
  bind the wizard to a public interface.
- `main()` maps `SystemExit(0)` to exit code 1 (`exc.code or EXIT_ERROR`), so
  helpers must return `EXIT_OK` rather than raise it.
