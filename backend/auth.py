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
from typing import Any, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

import db

log = logging.getLogger("auth")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SECRET = os.environ.get("ASR_AGENT_SECRET") or secrets.token_urlsafe(48)
TOKEN_TTL_SEC = int(os.environ.get("ASR_AGENT_TOKEN_TTL", str(60 * 60 * 12)))
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
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    return _user_to_dict(row)


def get_user(user_id: int) -> dict | None:
    with db.connect() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _user_to_dict(row) if row else None


def ensure_default_admin(*, username: str = "admin",
                        password: str = "admin") -> None:
    """Create a default admin account on first start if no users exist.

    Intended for local single-user installs. The credentials can be
    overridden via ``ASR_AGENT_ADMIN_USER`` / ``ASR_AGENT_ADMIN_PASSWORD``.
    Bypasses the public min-length checks so the convenience default
    ``admin``/``admin`` works out of the box.
    """
    username = os.environ.get("ASR_AGENT_ADMIN_USER", username).strip().lower()
    password = os.environ.get("ASR_AGENT_ADMIN_PASSWORD", password)
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if n > 0:
            return
        try:
            c.execute(
                "INSERT INTO users(username,email,password_hash,role,created_at) "
                "VALUES (?,?,?,?,?)",
                (username, None, hash_password(password), "admin", db.now()),
            )
            log.warning("seeded default admin user '%s' (change the password!)",
                        username)
        except Exception as e:  # noqa: BLE001
            log.warning("could not seed admin user: %s", e)


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
