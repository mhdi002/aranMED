"""User accounts, password hashing, and JWT-style session tokens.

We use stdlib only (``hashlib.scrypt`` + ``hmac`` + base64) so the project
keeps zero new dependencies for auth.  The token format is a tiny JWS-like
``base64url(header).base64url(payload).base64url(hmac_sha256_sig)`` triple
with HS256 — identical on the wire to JWT but produced by ~30 lines of
code.  ``python-jose`` / ``pyjwt`` are optional.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

import db
import throttle

log = logging.getLogger("auth")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SECRET = os.environ.get("ASR_AGENT_SECRET") or ""
if not SECRET:
    # Every worker generating its own random secret means a token minted by
    # one replica is rejected by the next -- users get random 401s the moment
    # you scale past one process, and every restart logs everyone out. Loud
    # warning rather than a hard failure so single-process dev still works.
    SECRET = secrets.token_urlsafe(48)
    log.warning(
        "ASR_AGENT_SECRET is not set — generated an ephemeral per-process secret. "
        "Tokens will not validate across replicas or survive a restart. "
        "Set ASR_AGENT_SECRET in .env before running more than one backend worker."
    )
TOKEN_TTL_SEC = int(os.environ.get("ASR_AGENT_TOKEN_TTL", str(60 * 60 * 12)))

# Brute-force throttling for the login endpoint. The counting logic and its
# storage live in backend/throttle.py, which defaults to a backend shared
# across replicas (SQLite, or Redis when REDIS_URL is set) -- per-process
# counters would hand an attacker N times the attempt budget on an N-replica
# deployment, since the gateway spreads their guesses across all of them.
# These thin aliases keep auth.* as the single import surface for callers.
LOGIN_MAX_ATTEMPTS = throttle.MAX_ATTEMPTS
LOGIN_WINDOW_SEC = throttle.WINDOW_SEC
LOGIN_LOCKOUT_SEC = throttle.LOCKOUT_SEC

throttle_key = throttle.make_key
login_is_locked = throttle.seconds_locked
record_login_failure = throttle.record_failure
record_login_success = throttle.record_success
# scrypt cost — n=2**14 ≈ 50 ms on a 2024 desktop, fine for an on-prem app.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LEN = 32

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                       n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_LEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(h)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
        h = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                           n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(h, expected)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# JWT-ish token (HS256)
# ---------------------------------------------------------------------------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad))


def create_token(payload: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"iat": int(time.time()),
               "exp": int(time.time()) + TOKEN_TTL_SEC, **payload}
    h = _b64(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(SECRET.encode("utf-8"),
                        f"{h}.{p}".encode("ascii"),
                        hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"


def decode_token(token: str) -> dict:
    try:
        h, p, sig = token.split(".")
    except ValueError as e:
        raise ValueError("malformed token") from e
    expected = _b64(hmac.new(SECRET.encode("utf-8"),
                             f"{h}.{p}".encode("ascii"),
                             hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        raise ValueError("bad signature")
    payload = json.loads(_b64d(p))
    if payload.get("exp", 0) < time.time():
        raise ValueError("token expired")
    return payload


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------
# Fixed dummy hash of a random password, used to equalise the timing of a
# "no such user" login against a real password check (see authenticate()).
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def create_user(*, username: str, password: str, email: str | None = None,
                role: str = "doctor") -> dict:
    username = username.strip().lower()
    if not username or len(username) < 3:
        raise ValueError("username must be at least 3 characters")
    if len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    if role not in ("doctor", "student", "resident", "admin", "radiologist"):
        raise ValueError("invalid role")
    with db.connect() as c:
        try:
            c.execute(
                "INSERT INTO users(username,email,password_hash,role,created_at) "
                "VALUES (?,?,?,?,?)",
                (username, email, hash_password(password), role, db.now()),
            )
        except db.sqlite3.IntegrityError as e:  # type: ignore[attr-defined]
            raise ValueError("username or email already in use") from e
        row = c.execute("SELECT * FROM users WHERE username=?",
                        (username,)).fetchone()
    return _user_to_dict(row)


def authenticate(*, username: str, password: str) -> dict | None:
    with db.connect() as c:
        row = c.execute("SELECT * FROM users WHERE username=?",
                        (username.strip().lower(),)).fetchone()
    if row is None:
        # Burn an equivalent scrypt round against a dummy hash before failing.
        # Returning early here is measurably faster than the password-check
        # path, which leaks whether a username exists to anyone timing the
        # endpoint.
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return _user_to_dict(row)


def get_user(user_id: int) -> dict | None:
    with db.connect() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _user_to_dict(row) if row else None


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def ensure_default_admin(*, username: str = "admin",
                        password: str | None = None) -> None:
    """Create the first admin account on first start if no users exist.

    There is no well-known default password. Precedence:

    1. ``ASR_AGENT_ADMIN_PASSWORD`` — what a real deployment should set.
    2. ``ASR_AGENT_ALLOW_INSECURE_ADMIN=1`` — opt in to the old
       ``admin``/``admin`` for throwaway local work. Must be explicit.
    3. Otherwise a random password is generated and written to
       ``ADMIN_CREDENTIALS_FILE`` (default ``<data>/initial-admin-password.txt``,
       mode 0600) and logged once.

    A seeded-but-unknown password is recoverable (delete the file's user row
    or set the env var and re-seed); a seeded *guessable* password on an
    internet-reachable deployment is not recoverable at all, which is why
    the guessable one now has to be asked for by name.
    """
    username = os.environ.get("ASR_AGENT_ADMIN_USER", username).strip().lower()
    env_password = os.environ.get("ASR_AGENT_ADMIN_PASSWORD") or password
    allow_insecure = _truthy(os.environ.get("ASR_AGENT_ALLOW_INSECURE_ADMIN", ""))

    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if n > 0:
            return

    generated = False
    if env_password:
        secret = env_password
    elif allow_insecure:
        secret = "admin"
        log.warning(
            "ASR_AGENT_ALLOW_INSECURE_ADMIN is set — seeding admin/admin. "
            "Never use this on a network-reachable deployment."
        )
    else:
        secret = secrets.token_urlsafe(18)
        generated = True

    try:
        with db.connect() as c:
            c.execute(
                "INSERT INTO users(username,email,password_hash,role,created_at) "
                "VALUES (?,?,?,?,?)",
                (username, None, hash_password(secret), "admin", db.now()),
            )
    except Exception as e:  # noqa: BLE001
        log.warning("could not seed admin user: %s", e)
        return

    if not generated:
        log.warning("seeded admin user %r from configured credentials", username)
        return

    path = _admin_credentials_path()
    written = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"username: {username}\npassword: {secret}\n\n"
            "Generated on first start because ASR_AGENT_ADMIN_PASSWORD was not set.\n"
            "Sign in, change the password, then delete this file.\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        written = True
    except OSError as e:
        log.warning("could not write admin credentials file %s: %s", path, e)

    log.warning(
        "No ASR_AGENT_ADMIN_PASSWORD set — generated a random password for %r. %s",
        username,
        f"Saved to {path} (delete it after first sign-in)." if written
        else f"Password (store it now, it is not saved anywhere): {secret}",
    )


def _admin_credentials_path() -> Path:
    override = os.environ.get("ADMIN_CREDENTIALS_FILE", "").strip()
    if override:
        return Path(override)
    return db.get_db_path().parent / "initial-admin-password.txt"


def _user_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "role": row["role"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------
def current_user(token: Optional[str] = Depends(oauth2_scheme)) -> dict:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = decode_token(token)
    except ValueError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e),
                            headers={"WWW-Authenticate": "Bearer"}) from e
    user = get_user(int(payload["sub"]))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found")
    return user


def current_user_optional(token: Optional[str] = Depends(oauth2_scheme)) -> dict | None:
    """Like :func:`current_user` but returns ``None`` instead of raising when
    no token is present — for endpoints (e.g. ``/api/chat``) that must keep
    working unauthenticated but should scope per-user data whenever a valid
    token *is* supplied, rather than always falling back to a shared owner.
    """
    if not token:
        return None
    try:
        payload = decode_token(token)
    except ValueError:
        return None
    return get_user(int(payload["sub"]))
