"""HTTP server for the local outreach wizard.

Standard-library only: ``http.server.ThreadingHTTPServer`` plus a tiny regex
router. It binds to loopback by default and layers four cheap protections on
top of the JSON API:

* a per-process token that the browser reads from a ``<meta>`` tag and echoes
  in ``X-Web-Token`` (blocks cross-site request forgery),
* a ``Host`` allow-list (blocks DNS-rebinding),
* path containment for static files (blocks traversal),
* an email + password login with TOTP two-factor (see :mod:`webapp.auth`) on
  every ``/api/*`` route except the handful needed to sign in.

Both credentials are required for data routes: the cookie proves *who* is
asking, the header proves the request came from a page this server rendered.
Neither alone is sufficient.

Long operations are delegated to :mod:`webapp.jobs`; the browser polls for
status so requests stay short.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import secrets
import threading
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Pattern, Tuple

from config_loader import ConfigError
from logging_setup import get_logger, setup_logging
from models import CHANNELS, STATUSES
from tracker import TrackerError
from webapp import auth
from webapp.auth import AuthError

from webapp import service
from webapp.jobs import JobManager
from webapp.profiles import ProfileStore, preset_catalog

LOG = get_logger("webapp")
VERSION = "1.1.0"

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
_MAX_BODY = 1_000_000
_PID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# Application state
# --------------------------------------------------------------------------- #


class App:
    def __init__(self, base_dir: str | Path, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.host = host
        self.port = int(port)
        self.profiles = ProfileStore(self.base_dir)
        self.jobs = JobManager()
        self.web_token = secrets.token_urlsafe(24)
        self.static_dir = Path(__file__).resolve().parent / "static"
        self.allowed_hosts = set(_LOOPBACK_HOSTS)
        if host not in {"0.0.0.0", "::", ""} and host not in self.allowed_hosts:
            self.allowed_hosts.add(host)
        self.auth = auth.UserStore(self.base_dir / auth.USERS_FILENAME)
        self.sessions = auth.SessionStore()
        self.login_throttle = auth.LoginThrottle()
        self.pending_totp: Dict[str, str] = {}

    # -- profile helpers --------------------------------------------------- #

    def require_profile(self, pid: str) -> str:
        if not pid or pid in {".", ".."} or not _PID_RE.match(pid) or not self.profiles.exists(pid):
            raise ApiError(f"unknown profile {pid!r}", 404)
        return pid


# --------------------------------------------------------------------------- #
# Request handler
# --------------------------------------------------------------------------- #


def make_handler(app: App) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"OutreachWizard/{VERSION}"
        app = None  # type: ignore[assignment]
        # Replaced with a real list at the top of every _handle call. Immutable
        # here on purpose: a stray append must fail loudly rather than leak one
        # request's Set-Cookie into the next.
        _extra_headers: List[Tuple[str, str]] = []  # noqa: RUF012

        # -- logging ----------------------------------------------------- #

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            LOG.debug("%s - %s", self.address_string(), fmt % args)

        # -- verbs ------------------------------------------------------- #

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

        # -- dispatch ---------------------------------------------------- #

        def _handle(self, method: str) -> None:
            self._extra_headers = []
            try:
                if not self._host_allowed():
                    self._send_json({"error": "host not allowed"}, 403)
                    return
                path = self.path.split("?", 1)[0]
                if path == "/" or path == "/index.html" or path.startswith("/static/"):
                    if method != "GET":
                        self._send_json({"error": "method not allowed"}, 405)
                        return
                    self._serve_static(path)
                    return
                if path.startswith("/api/"):
                    self._handle_api(method, path)
                    return
                self._send_json({"error": "not found"}, 404)
            except AuthError as exc:
                self._send_json({"error": str(exc)}, 400)
            except ApiError as exc:
                self._send_json({"error": str(exc)}, exc.status)
            except ConfigError as exc:
                self._send_json({"error": str(exc)}, 400)
            except TrackerError as exc:
                self._send_json({"error": str(exc)}, 400)
            except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                pass
            except Exception as exc:  # noqa: BLE001 - last-resort guard
                LOG.exception("Unhandled UI request error")
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        # -- security ---------------------------------------------------- #

        def _host_allowed(self) -> bool:
            host = self.headers.get("Host", "")
            hostname = _hostname(host)
            if hostname not in self.app.allowed_hosts:
                return False
            origin = self.headers.get("Origin")
            if origin:
                if _hostname(_origin_host(origin)) not in self.app.allowed_hosts:
                    return False
            return True

        def _authorized(self) -> bool:
            supplied = self.headers.get("X-Web-Token", "")
            return bool(supplied) and hmac.compare_digest(supplied, self.app.web_token)

        def _session(self) -> Optional[Dict[str, Any]]:
            raw = self.headers.get("Cookie", "")
            if not raw:
                return None
            try:
                jar = SimpleCookie()
                jar.load(raw)
            except Exception:  # noqa: BLE001 - a malformed cookie is just "no session"
                return None
            morsel = jar.get(auth.SESSION_COOKIE)
            return self.app.sessions.get(morsel.value) if morsel else None

        def _set_session_cookie(self, token: str, max_age: int) -> None:
            self._extra_headers.append(
                (
                    "Set-Cookie",
                    f"{auth.SESSION_COOKIE}={token}; Path=/; Max-Age={max_age}; "
                    "HttpOnly; SameSite=Strict",
                )
            )

        def _session_after_login(self, email: str) -> Dict[str, Any]:
            """Mint a session, attach the cookie and note the successful login."""
            token, ttl = self.app.sessions.create(email)
            self.app.auth.record_success(email)
            self._set_session_cookie(token, ttl)
            LOG.info("Signed in %s", auth.normalize_email(email))
            return {"email": auth.normalize_email(email), "expires_in": ttl}

        def _clear_session_cookie(self) -> None:
            self._extra_headers.append(
                ("Set-Cookie", f"{auth.SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
            )

        # -- API routing ------------------------------------------------- #

        def _handle_api(self, method: str, path: str) -> None:
            if not self._authorized():
                self._send_json({"error": "unauthorized"}, 401)
                return
            for route_method, pattern, func in PUBLIC_ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(path)
                if match:
                    result = func(self, **match.groupdict())
                    if result is not None:
                        self._send_json(result)
                    return
            if self._session() is None:
                self._send_json({"error": "authentication required", "authenticated": False}, 401)
                return
            for route_method, pattern, func in ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(path)
                if match:
                    result = func(self, **match.groupdict())
                    if result is not None:
                        self._send_json(result)
                    return
            self._send_json({"error": "not found"}, 404)

        # -- body / responses -------------------------------------------- #

        def _read_json(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > _MAX_BODY:
                raise ApiError("request body too large", 413)
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ApiError(f"invalid JSON body: {exc}") from exc
            if not isinstance(payload, dict):
                raise ApiError("request body must be a JSON object")
            return payload

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for name, value in self._extra_headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        # -- static ------------------------------------------------------ #

        def _serve_static(self, path: str) -> None:
            if path in {"/", "/index.html"}:
                rel = "index.html"
            elif path.startswith("/static/"):
                rel = path[len("/static/"):]
            else:
                self._send_json({"error": "not found"}, 404)
                return
            root = self.app.static_dir.resolve()
            target = (root / rel).resolve()
            if root != target and root not in target.parents:
                self._send_json({"error": "forbidden"}, 403)
                return
            if not target.is_file():
                self._send_json({"error": "not found"}, 404)
                return
            data = target.read_bytes()
            if target.name == "index.html":
                data = data.replace(b"__WEB_TOKEN__", self.app.web_token.encode("utf-8"))
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
                ctype += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: https:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'",
            )
            self.end_headers()
            self.wfile.write(data)

    Handler.app = app  # type: ignore[attr-defined]
    return Handler


def _hostname(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if ":" in value else value


def _origin_host(origin: str) -> str:
    text = (origin or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0]


# --------------------------------------------------------------------------- #
# Auth endpoints
# --------------------------------------------------------------------------- #


def _auth_state(handler, **_):
    """Everything the sign-in screen needs, and nothing an outsider should want."""
    session = handler._session()
    store = handler.app.auth
    payload: Dict[str, Any] = {
        "authenticated": session is not None,
        "accounts_exist": len(store) > 0,
        "password_min_length": auth.MIN_PASSWORD_LEN,
        "totp_digits": auth.TOTP_DIGITS,
        "recovery_code_count": auth.RECOVERY_CODE_COUNT,
    }
    if session is not None:
        payload["user"] = store.describe(session["email"])
    return payload


def _auth_setup(handler, **_):
    """Create the very first account. Refused once any account exists."""
    app = handler.app
    if len(app.auth):
        raise ApiError("accounts already exist - sign in instead", 409)
    app.login_throttle.check(handler.client_address[0])
    body = handler._read_json()
    email = str(body.get("email") or "")
    password = str(body.get("password") or "")
    app.auth.add(email, password, confirmation=str(body.get("confirm") or ""))
    session = handler._session_after_login(email)
    return {"ok": True, "user": app.auth.describe(email), "session": session}


def _auth_login(handler, **_):
    """Step one: email + password. May return a challenge for step two."""
    app = handler.app
    app.login_throttle.check(handler.client_address[0])
    body = handler._read_json()
    email = auth.normalize_email(str(body.get("email") or ""))
    if not email:
        raise AuthError("email is required")
    app.auth.check_password(email, str(body.get("password") or ""))
    if app.auth.has_totp(email):
        return {"totp_required": True, "challenge": app.sessions.begin_challenge(email)}
    session = handler._session_after_login(email)
    return {"totp_required": False, "user": app.auth.describe(email), "session": session}


def _auth_totp(handler, **_):
    """Step two: the six-digit code, or a recovery code."""
    app = handler.app
    app.login_throttle.check(handler.client_address[0])
    body = handler._read_json()
    email = auth.normalize_email(str(body.get("email") or ""))
    challenge = str(body.get("challenge") or "")
    code = str(body.get("code") or "")
    if not app.sessions.take_challenge(challenge, email):
        raise AuthError("this sign-in attempt expired - start again")
    remaining = app.auth.check_second_factor(email, code)
    app.auth.record_success(email)
    session = handler._session_after_login(email)
    payload: Dict[str, Any] = {
        "ok": True,
        "user": app.auth.describe(email),
        "session": session,
    }
    if remaining is not None:
        payload["recovery_codes_left"] = len(remaining)
        LOG.warning("%s signed in with a recovery code; %d remain", email, len(remaining))
    return payload


def _auth_logout(handler, **_):
    raw = handler.headers.get("Cookie", "")
    if raw:
        try:
            jar = SimpleCookie()
            jar.load(raw)
            morsel = jar.get(auth.SESSION_COOKIE)
            if morsel:
                handler.app.sessions.destroy(morsel.value)
        except Exception:  # noqa: BLE001 - nothing to clean up if the cookie is malformed
            pass
    handler._clear_session_cookie()
    return {"ok": True}


def _auth_change_password(handler, **_):
    app = handler.app
    session = handler._session()
    if session is None:
        raise ApiError("authentication required", 401)
    body = handler._read_json()
    email = session["email"]
    app.auth.check_password(email, str(body.get("current") or ""))
    app.auth.set_password(
        email,
        str(body.get("next") or ""),
        confirmation=str(body.get("confirm") or ""),
    )
    LOG.info("Password changed for %s", email)
    return {"ok": True}


def _auth_totp_begin(handler, **_):
    """Mint a candidate secret and hand back an otpauth:// URI to import."""
    app = handler.app
    session = handler._session()
    if session is None:
        raise ApiError("authentication required", 401)
    email = session["email"]
    secret = auth.generate_totp_secret()
    app.pending_totp[email] = secret
    return {"secret": secret, "uri": auth.totp_uri(secret, email), "digits": auth.TOTP_DIGITS}


def _auth_totp_confirm(handler, **_):
    """Prove the authenticator app is in sync, then commit the secret."""
    app = handler.app
    session = handler._session()
    if session is None:
        raise ApiError("authentication required", 401)
    body = handler._read_json()
    email = session["email"]
    secret = app.pending_totp.pop(email, "")
    if not secret:
        raise AuthError("start the setup again - no pending secret")
    if not auth.verify_totp(secret, str(body.get("code") or "")):
        app.pending_totp[email] = secret
        raise AuthError("that code does not match - check the time on your device")
    app.auth.enrol_totp(email, secret)
    codes = auth.generate_recovery_codes()
    app.auth.set_recovery_codes(email, codes)
    LOG.info("Enrolled two-factor for %s", email)
    return {"ok": True, "user": app.auth.describe(email), "recovery_codes": codes}


def _auth_totp_disable(handler, **_):
    app = handler.app
    session = handler._session()
    if session is None:
        raise ApiError("authentication required", 401)
    body = handler._read_json()
    email = session["email"]
    app.auth.check_password(email, str(body.get("password") or ""))
    if app.auth.has_totp(email):
        app.auth.check_second_factor(email, str(body.get("code") or ""))
    app.auth.disable_totp(email)
    return {"ok": True, "user": app.auth.describe(email)}


# --------------------------------------------------------------------------- #
# Endpoint implementations
# --------------------------------------------------------------------------- #


def _bootstrap(handler, **_):
    app = handler.app
    return {
        "version": VERSION,
        "presets": preset_catalog(),
        "profiles": [profile.describe() for profile in app.profiles.list()],
        "channels": list(CHANNELS),
        "statuses": list(STATUSES),
        "github": service.github_status(),
    }


def _presets(handler, **_):
    return {"presets": preset_catalog(), "channels": list(CHANNELS), "statuses": list(STATUSES)}


def _profiles_list(handler, **_):
    app = handler.app
    return {"profiles": [profile.describe() for profile in app.profiles.list()]}


def _profiles_create(handler, **_):
    app = handler.app
    body = handler._read_json()
    name = str(body.get("name") or "").strip()
    if not name:
        raise ApiError("a profile name is required")
    preset = str(body.get("preset") or "generic")
    try:
        profile = app.profiles.create(name, preset)
    except FileExistsError as exc:
        raise ApiError(str(exc), 409) from exc
    return {"profile": profile.describe()}


def _profile_get(handler, pid: str, **_):
    app = handler.app
    app.require_profile(pid)
    profile = app.profiles.get(pid)
    assert profile is not None
    return {"profile": profile.describe(), "config": profile.data}


def _profile_update(handler, pid: str, **_):
    app = handler.app
    app.require_profile(pid)
    body = handler._read_json()
    data = body.get("config")
    if not isinstance(data, dict):
        raise ApiError("body must contain a 'config' object")
    errors = app.profiles.validate(pid, data)
    if errors:
        return {"ok": False, "errors": errors}
    app.profiles.write(pid, data)
    profile = app.profiles.get(pid)
    warnings: List[str] = []
    try:
        warnings = list(app.profiles.load_config(pid).warnings)
    except ConfigError as exc:
        warnings = [str(exc)]
    return {"ok": True, "errors": [], "warnings": warnings, "profile": profile.describe() if profile else {}}


def _profile_delete(handler, pid: str, **_):
    app = handler.app
    app.require_profile(pid)
    app.profiles.delete(pid)
    return {"ok": True, "deleted": pid}


def _profile_status(handler, pid: str, **_):
    app = handler.app
    app.require_profile(pid)
    config = app.profiles.load_config(pid)
    tracker = service.build_tracker(config)
    return service.status(config, tracker)


def _profile_sponsors(handler, pid: str, **_):
    app = handler.app
    app.require_profile(pid)
    config = app.profiles.load_config(pid)
    tracker = service.build_tracker(config)
    sponsors = [sponsor.to_dict() for sponsor in tracker.all()]
    return {
        "summary": tracker.summary(),
        "sponsors": sponsors,
        "channels": list(CHANNELS),
        "statuses": list(STATUSES),
    }


def _profile_add_sponsor(handler, pid: str, **_):
    app = handler.app
    app.require_profile(pid)
    body = handler._read_json()
    config = app.profiles.load_config(pid)
    return service.add_sponsor(config, body)


def _profile_mark(handler, pid: str, **_):
    app = handler.app
    app.require_profile(pid)
    body = handler._read_json()
    name = str(body.get("name") or "").strip()
    status = str(body.get("status") or "").strip().lower()
    if not name or not status:
        raise ApiError("both 'name' and 'status' are required")
    config = app.profiles.load_config(pid)
    return service.mark_sponsor(config, name, status)


def _profile_remove(handler, pid: str, **_):
    app = handler.app
    app.require_profile(pid)
    body = handler._read_json()
    name = str(body.get("name") or "").strip()
    if not name:
        raise ApiError("'name' is required")
    config = app.profiles.load_config(pid)
    return service.remove_sponsor(config, name)


def _profile_candidates(handler, pid: str, **_):
    app = handler.app
    app.require_profile(pid)
    config = app.profiles.load_config(pid)
    return service.load_cached_candidates(config)


def _profile_discover(handler, pid: str, **_):
    app = handler.app
    app.require_profile(pid)
    body = handler._read_json()
    config = app.profiles.load_config(pid)

    topics = body.get("topics")
    if isinstance(topics, str):
        topics = [part.strip() for part in topics.split(",") if part.strip()]

    def work(ctx):
        return service.run_discovery(
            config,
            ctx,
            topics=topics,
            min_org_repos=body.get("min_org_repos"),
            min_individual_genesys_repos=body.get("min_individual_genesys_repos"),
            min_stars=body.get("min_stars"),
            exclude_forks=body.get("exclude_forks"),
            per_page=body.get("per_page"),
            max_pages=body.get("max_pages"),
            max_owner_lookups=body.get("max_owner_lookups"),
            scrape_public_emails=body.get("scrape_public_emails"),
            email_scrape_max_sites=body.get("email_scrape_max_sites"),
        )

    job = app.jobs.start("discovery", f"Discover ({pid})", work)
    return {"job": job.to_dict(), "profile": pid}


def _profile_approve(handler, pid: str, **_):
    app = handler.app
    app.require_profile(pid)
    body = handler._read_json()
    candidates = body.get("candidates")
    if not isinstance(candidates, list):
        raise ApiError("body must contain a 'candidates' list")
    config = app.profiles.load_config(pid)
    return service.approve_candidates(config, candidates, channel=str(body.get("channel") or "email"))


def _profile_preview(handler, pid: str, **_):
    app = handler.app
    app.require_profile(pid)
    body = handler._read_json()
    config = app.profiles.load_config(pid)
    return service.generate_preview(
        config,
        str(body.get("sponsor") or ""),
        channel=body.get("channel"),
        use_llm=bool(body.get("llm")),
    )


def _profile_send(handler, pid: str, **_):
    app = handler.app
    app.require_profile(pid)
    body = handler._read_json()
    config = app.profiles.load_config(pid)
    names = body.get("names")
    if names is not None and not isinstance(names, list):
        raise ApiError("'names' must be a list of sponsor names")
    dry_run = body.get("dry_run")
    dry_run = True if dry_run is None else bool(dry_run)

    def work(ctx):
        return service.send_sponsors(
            config,
            ctx,
            names=[str(name) for name in names] if names else None,
            dry_run=dry_run,
            use_llm=bool(body.get("llm")),
            batch=int(body["batch"]) if body.get("batch") else None,
        )

    label = f"{'Dry run' if dry_run else 'Send'} ({pid})"
    job = app.jobs.start("send", label, work)
    return {"job": job.to_dict(), "profile": pid}


def _profile_secrets(handler, pid: str, **_):
    app = handler.app
    app.require_profile(pid)
    body = handler._read_json()
    config = app.profiles.load_config(pid)
    return service.set_secrets(
        config,
        smtp_password=str(body.get("smtp_password") or ""),
        forum_api_key=str(body.get("forum_api_key") or ""),
        github_token=str(body.get("github_token") or ""),
    )


def _github_connect(handler, **_):
    body = handler._read_json()
    token = str(body.get("token") or "")
    token_env = str(body.get("token_env") or "GITHUB_TOKEN")
    api_base = str(body.get("api_base") or "https://api.github.com")
    return service.connect_github(token=token, token_env=token_env, api_base=api_base)


def _github_disconnect(handler, **_):
    body = handler._read_json()
    return service.disconnect_github(str(body.get("token_env") or "GITHUB_TOKEN"))


def _github_status(handler, **kwargs):
    app = handler.app
    pid = kwargs.get("pid")
    config = None
    if pid and app.profiles.exists(pid):
        config = app.profiles.load_config(pid)
    return service.github_status(config)


def _jobs_list(handler, **_):
    return {"jobs": [job.to_dict(since=max(len(job.logs) - 5, 0)) for job in handler.app.jobs.list()]}


def _job_get(handler, jid: str, **_):
    job = handler.app.jobs.get(jid)
    if job is None:
        raise ApiError("unknown job", 404)
    query = handler.path.split("?", 1)[1] if "?" in handler.path else ""
    since = 0
    for part in query.split("&"):
        if part.startswith("since="):
            try:
                since = int(part.split("=", 1)[1])
            except ValueError:
                since = 0
    return {"job": job.to_dict(since=since)}


def _job_cancel(handler, jid: str, **_):
    if not handler.app.jobs.cancel(jid):
        raise ApiError("unknown job", 404)
    return {"ok": True, "cancelled": jid}


#: Routes reachable without a session. Only what signing in needs.
PUBLIC_ROUTES: List[Tuple[str, Pattern[str], Callable[..., Any]]] = [
    ("GET", re.compile(r"^/api/auth/state$"), _auth_state),
    ("POST", re.compile(r"^/api/auth/setup$"), _auth_setup),
    ("POST", re.compile(r"^/api/auth/login$"), _auth_login),
    ("POST", re.compile(r"^/api/auth/totp$"), _auth_totp),
    ("POST", re.compile(r"^/api/auth/logout$"), _auth_logout),
]

ROUTES: List[Tuple[str, Pattern[str], Callable[..., Any]]] = [
    ("GET", re.compile(r"^/api/bootstrap$"), _bootstrap),
    ("GET", re.compile(r"^/api/presets$"), _presets),
    ("GET", re.compile(r"^/api/profiles$"), _profiles_list),
    ("POST", re.compile(r"^/api/profiles$"), _profiles_create),
    ("GET", re.compile(r"^/api/profiles/(?P<pid>[^/]+)$"), _profile_get),
    ("PUT", re.compile(r"^/api/profiles/(?P<pid>[^/]+)$"), _profile_update),
    ("DELETE", re.compile(r"^/api/profiles/(?P<pid>[^/]+)$"), _profile_delete),
    ("GET", re.compile(r"^/api/profiles/(?P<pid>[^/]+)/status$"), _profile_status),
    ("GET", re.compile(r"^/api/profiles/(?P<pid>[^/]+)/sponsors$"), _profile_sponsors),
    ("POST", re.compile(r"^/api/profiles/(?P<pid>[^/]+)/sponsors$"), _profile_add_sponsor),
    ("POST", re.compile(r"^/api/profiles/(?P<pid>[^/]+)/mark$"), _profile_mark),
    ("POST", re.compile(r"^/api/profiles/(?P<pid>[^/]+)/remove$"), _profile_remove),
    ("GET", re.compile(r"^/api/profiles/(?P<pid>[^/]+)/candidates$"), _profile_candidates),
    ("POST", re.compile(r"^/api/profiles/(?P<pid>[^/]+)/discover$"), _profile_discover),
    ("POST", re.compile(r"^/api/profiles/(?P<pid>[^/]+)/approve$"), _profile_approve),
    ("POST", re.compile(r"^/api/profiles/(?P<pid>[^/]+)/preview$"), _profile_preview),
    ("POST", re.compile(r"^/api/profiles/(?P<pid>[^/]+)/send$"), _profile_send),
    ("POST", re.compile(r"^/api/profiles/(?P<pid>[^/]+)/secrets$"), _profile_secrets),
    ("GET", re.compile(r"^/api/profiles/(?P<pid>[^/]+)/github$"), _github_status),
    ("POST", re.compile(r"^/api/github/connect$"), _github_connect),
    ("POST", re.compile(r"^/api/github/disconnect$"), _github_disconnect),
    ("GET", re.compile(r"^/api/github/status$"), _github_status),
    ("GET", re.compile(r"^/api/jobs$"), _jobs_list),
    ("GET", re.compile(r"^/api/jobs/(?P<jid>[^/]+)$"), _job_get),
    ("POST", re.compile(r"^/api/jobs/(?P<jid>[^/]+)/cancel$"), _job_cancel),
    ("POST", re.compile(r"^/api/auth/password$"), _auth_change_password),
    ("POST", re.compile(r"^/api/auth/totp/enrol$"), _auth_totp_begin),
    ("POST", re.compile(r"^/api/auth/totp/confirm$"), _auth_totp_confirm),
    ("POST", re.compile(r"^/api/auth/totp/disable$"), _auth_totp_disable),
]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


BANNER = r"""
  Outreach Wizard
  ---------------
"""


def run_ui(
    *,
    base_dir: Optional[str | Path] = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    base = Path(base_dir).expanduser().resolve() if base_dir else Path(__file__).resolve().parent.parent
    created: Optional[str]
    setup_logging(log_file=base / "ui.log", level="INFO", quiet=True)
    try:
        # After setup_logging so credential warnings reach ui.log as well.
        app = App(base, host=host, port=port)
        created = app.profiles.ensure_default()
    except AuthError as exc:
        print(f"Could not load UI accounts: {exc}")
        return 1

    handler = make_handler(app)
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        print(f"Could not bind {host}:{port} - {exc}")
        print("Is another instance already running? Try --port 8766.")
        return 1

    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(BANNER.strip())
    if created:
        print(f"Created initial profile: {created} (from your existing config)")
    if not len(app.auth):
        print("No accounts yet - the browser will ask you to create the first one.")
    else:
        print(f"Accounts: {', '.join(app.auth.emails())}")
        enrolled = [email for email in app.auth.emails() if app.auth.has_totp(email)]
        if enrolled:
            print(f"Two-factor: {'yes' if len(enrolled) == len(app.auth) else 'partly'}"
                  f" ({len(enrolled)}/{len(app.auth)} enrolled)")
    if not os.environ.get(auth.MASTER_ENV, "").strip():
        print(f"Warning: {auth.MASTER_ENV} is not set (see ui.log).")
    print(f"Serving at {url}")
    print("Press Ctrl+C to stop.\n")

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0
