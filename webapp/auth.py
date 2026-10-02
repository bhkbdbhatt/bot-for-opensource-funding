"""Email + password login with TOTP two-factor authentication.

Standard library only, matching the rest of the project. Nothing here talks to
the network, so the whole scheme is auditable in one file:

* **Passwords** are stretched with PBKDF2-HMAC-SHA256 (600k rounds, 16-byte
  random salt) and *peppered* with a key derived from an environment variable.
  The plaintext password is never stored or logged.
* **TOTP secrets** (RFC 6238, SHA-1, 6 digits, 30s step) are sealed at rest:
  an HMAC-SHA256 counter-mode keystream encrypts them and a separate HMAC key
  authenticates the envelope, so editing ``users.json`` does not let an
  attacker swap in their own secret.
* **Sessions** are server-side and in-memory only. The cookie carries a random
  token; the store keys sessions by its SHA-256 digest, so a memory dump yields
  no usable cookie.
* **Recovery codes** are single-use, stored hashed, and are the only way back in
  when the authenticator device is lost.

The master key comes from ``OUTREACH_AUTH_SECRET``. When it is unset a random
local key is generated once and stored inside ``users.json`` - that keeps the
feature usable with zero setup, but it protects the file no better than the
filesystem already does, so the server logs a warning suggesting the env var.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from logging_setup import get_logger
from models import is_valid_email, now_iso

LOG = get_logger("auth")

USERS_FILENAME = "users.json"
SCHEMA_VERSION = 1

#: Environment variable holding the master key used to pepper passwords and
#: seal TOTP secrets. Strongly recommended; without it a local key is generated.
MASTER_ENV = "OUTREACH_AUTH_SECRET"

PASSWORD_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ROUNDS = 600_000
PBKDF2_KEY_LEN = 32
SALT_BYTES = 16

MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 512

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1
RECOVERY_CODE_COUNT = 10

SESSION_COOKIE = "ow_session"
SESSION_TTL_SECONDS = 8 * 3600
CHALLENGE_TTL_SECONDS = 5 * 60

MAX_FAILED_LOGINS = 5
MAX_LOCK_SECONDS = 3600
LOGIN_RATE_LIMIT = 30
LOGIN_RATE_WINDOW = 60

_ENVELOPE_VERSION = "v1"
_B32_RE = re.compile(r"^[A-Z2-7]+=*$")


class AuthError(Exception):
    """Authentication failed. The message is safe to show to the user."""


class LockedOutError(AuthError):
    """Too many failed attempts; the account is temporarily unusable."""


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text + padding, validate=True)
    except (ValueError, TypeError) as exc:
        raise AuthError("stored credential is malformed") from exc


def hash_password(password: str, *, pepper: str = "", rounds: int = PBKDF2_ROUNDS) -> str:
    """Return ``pbkdf2_sha256$rounds$salt$hash``, all base64."""
    if len(password) > MAX_PASSWORD_LEN:
        raise AuthError(f"password must be at most {MAX_PASSWORD_LEN} characters")
    salt = os.urandom(SALT_BYTES)
    digest = _stretch(password, pepper=pepper, salt=salt, rounds=rounds)
    return f"{PASSWORD_ALGORITHM}${rounds}${_b64(salt)}${_b64(digest)}"


def _stretch(password: str, *, pepper: str, salt: bytes, rounds: int) -> bytes:
    material = pepper.encode("utf-8") + b"\x00" + password.encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", material, salt, rounds, PBKDF2_KEY_LEN)


def verify_password(password: str, encoded: str, *, pepper: str = "") -> bool:
    """Constant-time check of ``password`` against a stored hash."""
    if not encoded:
        return False
    parts = encoded.split("$")
    if len(parts) != 4 or parts[0] != PASSWORD_ALGORITHM:
        return False
    try:
        rounds = int(parts[1])
        salt = _unb64(parts[2])
        expected = _unb64(parts[3])
    except (ValueError, AuthError):
        return False
    if rounds < 1:
        return False
    candidate = _stretch(password, pepper=pepper, salt=salt, rounds=rounds)
    return hmac.compare_digest(candidate, expected)


def password_problem(password: str, confirmation: str = "") -> Optional[str]:
    """Return a human-readable reason the password is unacceptable, else ``None``."""
    if len(password) < MIN_PASSWORD_LEN:
        return f"password must be at least {MIN_PASSWORD_LEN} characters"
    if len(password) > MAX_PASSWORD_LEN:
        return f"password must be at most {MAX_PASSWORD_LEN} characters"
    if password.strip() != password:
        return "password must not start or end with whitespace"
    if confirmation and not hmac.compare_digest(password, confirmation):
        return "passwords do not match"
    classes = sum(
        bool(pattern.search(password))
        for pattern in (re.compile(r"[a-z]"), re.compile(r"[A-Z]"), re.compile(r"[0-9]"), re.compile(r"[^A-Za-z0-9]"))
    )
    if classes < 3:
        return "password must mix at least three of: lowercase, uppercase, digits, symbols"
    return None


# --------------------------------------------------------------------------- #
# TOTP (RFC 6238)
# --------------------------------------------------------------------------- #


def generate_totp_secret() -> str:
    """A 20-byte (160-bit) shared secret in unpadded base32."""
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def normalise_totp_secret(secret: str) -> str:
    """Upper-case and pad a user-typed base32 secret."""
    cleaned = re.sub(r"[\s-]+", "", secret or "").upper()
    if not cleaned or not _B32_RE.match(cleaned):
        raise AuthError("that is not a valid authenticator secret")
    cleaned = cleaned.rstrip("=")
    try:
        decoded = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8), casefold=True)
    except ValueError as exc:
        raise AuthError("that is not a valid authenticator secret") from exc
    if not decoded:
        raise AuthError("that is not a valid authenticator secret")
    return cleaned + "=" * (-len(cleaned) % 8)


def totp_code(secret: str, *, at: Optional[float] = None, step: int = TOTP_STEP_SECONDS) -> str:
    """The code an authenticator app shows at time ``at``."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8), casefold=True)
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def verify_totp(secret: str, code: str, *, at: Optional[float] = None, window: int = TOTP_WINDOW) -> bool:
    """Check ``code`` against the current step plus +/- ``window`` steps."""
    digits = (code or "").strip().replace(" ", "")
    if not digits.isdigit() or len(digits) != TOTP_DIGITS:
        return False
    for drift in range(-window, window + 1):
        expected = totp_code(secret, at=(time.time() if at is None else at) + drift * TOTP_STEP_SECONDS)
        if hmac.compare_digest(expected, digits):
            return True
    return False


def totp_uri(secret: str, email: str, *, issuer: str = "Outreach Wizard") -> str:
    """An ``otpauth://`` URI that authenticator apps can import by QR code."""
    label = quote(f"{issuer}:{email}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}"
    )


# --------------------------------------------------------------------------- #
# Sealed-at-rest storage for TOTP secrets
# --------------------------------------------------------------------------- #


def _derive(master: str, purpose: str) -> bytes:
    return hmac.new(master.encode("utf-8"), purpose.encode("ascii"), hashlib.sha256).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + struct.pack(">Q", counter), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def seal(plaintext: str, *, master: str, user_id: str) -> str:
    """Encrypt-then-MAC a TOTP secret into ``v1.<nonce>.<ct>.<mac>``."""
    data = plaintext.encode("utf-8")
    enc_key = _derive(master, f"{_ENVELOPE_VERSION}:enc")
    mac_key = _derive(master, f"{_ENVELOPE_VERSION}:mac")
    nonce = os.urandom(16)
    ciphertext = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    mac = hmac.new(mac_key, f"{_ENVELOPE_VERSION}:{user_id}:".encode("utf-8") + nonce + ciphertext, hashlib.sha256).digest()
    return ".".join((_ENVELOPE_VERSION, _b64(nonce), _b64(ciphertext), _b64(mac)))


def unseal(envelope: str, *, master: str, user_id: str) -> str:
    """Reverse :func:`seal`, rejecting any envelope whose MAC does not verify."""
    parts = (envelope or "").split(".")
    if len(parts) != 4 or parts[0] != _ENVELOPE_VERSION:
        raise AuthError("stored two-factor envelope is malformed")
    try:
        nonce = _unb64(parts[1])
        ciphertext = _unb64(parts[2])
        mac = _unb64(parts[3])
    except AuthError:
        raise AuthError("stored two-factor envelope is malformed") from None
    mac_key = _derive(master, f"{_ENVELOPE_VERSION}:mac")
    expected = hmac.new(mac_key, f"{_ENVELOPE_VERSION}:{user_id}:".encode("utf-8") + nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):
        raise AuthError("stored two-factor secret failed integrity check")
    enc_key = _derive(master, f"{_ENVELOPE_VERSION}:enc")
    plain = bytes(a ^ b for a, b in zip(ciphertext, _keystream(enc_key, nonce, len(ciphertext))))
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuthError("stored two-factor secret is not readable") from exc


# --------------------------------------------------------------------------- #
# Recovery codes
# --------------------------------------------------------------------------- #


def _recovery_code() -> str:
    raw = secrets.token_hex(5)
    return f"{raw[:5]}-{raw[5:]}"


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> List[str]:
    return [_recovery_code() for _ in range(max(1, count))]


def _hash_code(code: str) -> str:
    normalised = re.sub(r"[\s-]+", "", (code or "").lower())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def consume_recovery_code(stored: Iterable[str], code: str) -> Optional[List[str]]:
    """Return the remaining codes if ``code`` matched, else ``None``."""
    candidate = _hash_code(code)
    remaining = list(stored)
    for index, hashed in enumerate(remaining):
        if hmac.compare_digest(str(hashed), candidate):
            remaining.pop(index)
            return remaining
    return None


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #


class UserStore:
    """Accounts on disk. Writes are atomic, mirroring :mod:`tracker`."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._users: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._local_key = ""
        self._master = ""
        self._load()

    # -- persistence ------------------------------------------------------- #

    def _state(self) -> Dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "updated_at": now_iso(),
            "local_key": self._local_key,
            "users": [self._users[email] for email in sorted(self._users)],
        }

    def _load(self) -> None:
        if not self.path.is_file():
            self._master = self._resolve_master("")
            return
        try:
            state = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise AuthError(
                f"{self.path} is corrupt ({exc}). Move it aside and re-create the accounts."
            ) from exc
        except OSError as exc:
            raise AuthError(f"cannot read {self.path}: {exc}") from exc
        if not isinstance(state, dict):
            raise AuthError(f"{self.path}: expected a JSON object at the top level")

        self._local_key = str(state.get("local_key") or "")
        for entry in state.get("users") or []:
            if not isinstance(entry, dict):
                continue
            email = normalize_email(str(entry.get("email") or ""))
            if not email:
                continue
            self._users[email] = {
                "email": email,
                "password": str(entry.get("password") or ""),
                "totp": str(entry.get("totp") or ""),
                "recovery": [str(item) for item in (entry.get("recovery") or []) if isinstance(item, str)],
                "created_at": str(entry.get("created_at") or now_iso()),
                "last_login": str(entry.get("last_login") or ""),
                "failed_attempts": int(entry.get("failed_attempts") or 0),
                "locked_until": float(entry.get("locked_until") or 0),
            }
        self._master = self._resolve_master(self._local_key)

    def _resolve_master(self, local_key: str) -> str:
        from_env = os.environ.get(MASTER_ENV, "").strip()
        if from_env:
            return from_env
        if local_key:
            LOG.warning(
                "%s is not set. Falling back to the key stored inside %s, which protects "
                "the file no better than the filesystem already does. Set it to a long "
                "random string to survive moving or backing up the file.",
                MASTER_ENV,
                self.path,
            )
            return local_key
        self._local_key = _b64(os.urandom(32))
        LOG.warning(
            "%s is not set; generated a local key in %s. Set %s to keep credentials valid "
            "across machines and to keep TOTP secrets out of the file in the clear.",
            MASTER_ENV,
            self.path,
            MASTER_ENV,
        )
        return self._local_key

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._state(), indent=2, ensure_ascii=False)
            handle = tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(self.path.parent),
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            )
            try:
                with handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(handle.name, self.path)
            except OSError as exc:
                try:
                    os.unlink(handle.name)
                except OSError:
                    pass
                raise AuthError(f"cannot write {self.path}: {exc}") from exc
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- lookups ----------------------------------------------------------- #

    def __len__(self) -> int:
        return len(self._users)

    def emails(self) -> List[str]:
        return sorted(self._users)

    def exists(self, email: str) -> bool:
        return normalize_email(email) in self._users

    def get(self, email: str) -> Optional[Dict[str, Any]]:
        user = self._users.get(normalize_email(email))
        return dict(user) if user else None

    def describe(self, email: str) -> Dict[str, Any]:
        user = self._users.get(normalize_email(email)) or {}
        return {
            "email": user.get("email", ""),
            "totp_enrolled": bool(user.get("totp")),
            "recovery_codes_left": len(user.get("recovery") or []),
            "created_at": user.get("created_at", ""),
            "last_login": user.get("last_login", ""),
        }

    # -- mutation ---------------------------------------------------------- #

    def add(self, email: str, password: str, *, confirmation: str = "") -> Dict[str, Any]:
        key = normalize_email(email)
        if not is_valid_email(key):
            raise AuthError("that does not look like a valid email address")
        problem = password_problem(password, confirmation)
        if problem:
            raise AuthError(problem)
        with self._lock:
            if key in self._users:
                raise AuthError(f"{key} already exists")
            self._users[key] = {
                "email": key,
                "password": hash_password(password, pepper=self._master),
                "totp": "",
                "recovery": [],
                "created_at": now_iso(),
                "last_login": "",
                "failed_attempts": 0,
                "locked_until": 0.0,
            }
            self.save()
        LOG.info("Created UI account for %s", key)
        return self.describe(key)

    def remove(self, email: str) -> None:
        key = normalize_email(email)
        with self._lock:
            if key not in self._users:
                raise AuthError(f"no such account: {key}")
            del self._users[key]
            self.save()
        LOG.info("Removed UI account %s", key)

    def set_password(self, email: str, password: str, *, confirmation: str = "") -> None:
        key = normalize_email(email)
        problem = password_problem(password, confirmation)
        if problem:
            raise AuthError(problem)
        with self._lock:
            if key not in self._users:
                raise AuthError(f"no such account: {key}")
            self._users[key]["password"] = hash_password(password, pepper=self._master)
            self._users[key]["failed_attempts"] = 0
            self._users[key]["locked_until"] = 0.0
            self.save()

    def enrol_totp(self, email: str, secret: str) -> None:
        key = normalize_email(email)
        normalised = normalise_totp_secret(secret)
        with self._lock:
            if key not in self._users:
                raise AuthError(f"no such account: {key}")
            self._users[key]["totp"] = seal(normalised, master=self._master, user_id=key)
            self._users[key]["recovery"] = []
            self.save()

    def set_recovery_codes(self, email: str, codes: Iterable[str]) -> List[str]:
        """Store ``codes`` hashed. The plaintext is never written down."""
        key = normalize_email(email)
        plaintext = [str(code) for code in codes]
        with self._lock:
            if key not in self._users:
                raise AuthError(f"no such account: {key}")
            self._users[key]["recovery"] = [_hash_code(code) for code in plaintext]
            self.save()
        return plaintext

    def disable_totp(self, email: str) -> None:
        key = normalize_email(email)
        with self._lock:
            if key not in self._users:
                raise AuthError(f"no such account: {key}")
            self._users[key]["totp"] = ""
            self._users[key]["recovery"] = []
            self.save()
        LOG.info("Disabled two-factor for %s", key)

    def _totp_secret(self, key: str) -> str:
        envelope = self._users[key].get("totp") or ""
        if not envelope:
            raise AuthError("two-factor is not enrolled for this account")
        return normalise_totp_secret(unseal(envelope, master=self._master, user_id=key))

    def has_totp(self, email: str) -> bool:
        return bool((self._users.get(normalize_email(email)) or {}).get("totp"))

    # -- credentials ------------------------------------------------------- #

    def check_password(self, email: str, password: str) -> None:
        """Raise unless the password is correct and the account is unlocked."""
        key = normalize_email(email)
        user = self._users.get(key)
        if user is None:
            # Spend the same work on an unknown address that a real check costs,
            # so the response time does not reveal which accounts exist.
            hash_password(password or "", pepper=self._master)
            raise AuthError("email or password is incorrect")
        locked_until = float(user.get("locked_until") or 0)
        if locked_until > time.time():
            raise LockedOutError(
                f"too many failed attempts; try again in {int(locked_until - time.time()) + 1}s"
            )
        if not verify_password(password or "", user["password"], pepper=self._master):
            self.record_failure(key)
            raise AuthError("email or password is incorrect")

    def check_second_factor(self, email: str, code: str) -> Optional[List[str]]:
        """Validate a TOTP or recovery code. Returns remaining recovery codes if one was used."""
        key = normalize_email(email)
        user = self._users.get(key)
        if user is None:
            raise AuthError("no such account")
        if not user.get("totp"):
            return None
        secret = self._totp_secret(key)
        if verify_totp(secret, code):
            return None
        remaining = consume_recovery_code(user.get("recovery") or [], code)
        if remaining is None:
            self.record_failure(key)
            raise AuthError("that two-factor code is not valid")
        with self._lock:
            user["recovery"] = remaining
            self.save()
        LOG.warning("Recovery code used for %s (%d left)", key, len(remaining))
        return remaining

    def record_failure(self, email: str) -> None:
        key = normalize_email(email)
        with self._lock:
            user = self._users.get(key)
            if user is None:
                return
            user["failed_attempts"] = int(user.get("failed_attempts") or 0) + 1
            if user["failed_attempts"] >= MAX_FAILED_LOGINS:
                backoff = min(2 ** (user["failed_attempts"] - MAX_FAILED_LOGINS + 3), MAX_LOCK_SECONDS)
                user["locked_until"] = time.time() + backoff
                LOG.warning("Locked %s for %ds after %d failed logins", key, backoff, user["failed_attempts"])
            self.save()

    def record_success(self, email: str) -> None:
        key = normalize_email(email)
        with self._lock:
            user = self._users.get(key)
            if user is None:
                return
            user["failed_attempts"] = 0
            user["locked_until"] = 0.0
            user["last_login"] = now_iso()
            self.save()


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


# --------------------------------------------------------------------------- #
# Sessions and pending challenges
# --------------------------------------------------------------------------- #


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionStore:
    """In-memory sessions keyed by token digest, plus pending 2FA challenges."""

    def __init__(self, ttl: int = SESSION_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._challenges: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def create(self, email: str) -> Tuple[str, int]:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._sessions[_digest(token)] = {"email": normalize_email(email), "expires": now + self.ttl}
            self._prune(now)
        return token, self.ttl

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(_digest(token))
            if session is None:
                return None
            if session["expires"] <= now:
                del self._sessions[_digest(token)]
                return None
            return dict(session)

    def destroy(self, token: str) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(_digest(token), None)

    def begin_challenge(self, email: str) -> str:
        """Park an email between the password step and the two-factor step."""
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._challenges[_digest(token)] = {
                "email": normalize_email(email),
                "expires": time.time() + CHALLENGE_TTL_SECONDS,
            }
            self._prune(time.time())
        return token

    def take_challenge(self, token: str, email: str) -> bool:
        """Validate a challenge without consuming it.

        It is deliberately not single-use: a mistyped code should not force the
        user to type their password again. Brute force is bounded instead by the
        five-minute challenge lifetime, :class:`LoginThrottle` and the lockout
        that :meth:`UserStore.record_failure` applies.
        """
        if not token:
            return False
        with self._lock:
            entry = self._challenges.get(_digest(token))
        if entry is None or entry["expires"] <= time.time():
            return False
        return hmac.compare_digest(entry["email"], normalize_email(email))

    def drop_challenge(self, token: str) -> None:
        if not token:
            return
        with self._lock:
            self._challenges.pop(_digest(token), None)

    def _prune(self, now: float) -> None:
        for key, session in list(self._sessions.items()):
            if session["expires"] <= now:
                del self._sessions[key]
        for key, challenge in list(self._challenges.items()):
            if challenge["expires"] <= now:
                del self._challenges[key]


class LoginThrottle:
    """Coarse per-client cap so a stolen loopback page cannot hammer passwords."""

    def __init__(self, limit: int = LOGIN_RATE_LIMIT, window: int = LOGIN_RATE_WINDOW) -> None:
        self.limit = limit
        self.window = window
        self._hits: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def check(self, client: str) -> None:
        now = time.time()
        key = client or "unknown"
        with self._lock:
            hits = [stamp for stamp in self._hits.get(key, []) if now - stamp < self.window]
            if len(hits) >= self.limit:
                raise LockedOutError("too many attempts; wait a moment and try again")
            hits.append(now)
            self._hits[key] = hits


# --------------------------------------------------------------------------- #
# Summary used by `status` and the UI banner
# --------------------------------------------------------------------------- #


def describe_store(store: UserStore) -> Dict[str, Any]:
    return {
        "file": str(store.path),
        "accounts": len(store),
        "master_env": MASTER_ENV,
        "master_env_set": bool(os.environ.get(MASTER_ENV, "").strip()),
    }