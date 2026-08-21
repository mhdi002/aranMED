"""Time-based one-time passwords (TOTP, RFC 6238) for second-factor login.

Implemented on stdlib ``hmac``/``hashlib``/``base64`` for the same reason the
rest of ``auth.py`` is: no new dependency to audit, and TOTP is ~30 lines.
Compatible with Google Authenticator, Authy, 1Password, FreeOTP — anything
that consumes an ``otpauth://`` URI.

Design notes:

* **Verification is constant-time** and checks a small window of adjacent
  steps, because client and server clocks drift. The window is configurable;
  wider is friendlier and slightly weaker.
* **Replay is blocked.** A code that verified once is remembered until its
  step expires, so an attacker who observes a code over the user's shoulder
  (or in a phished form) cannot reuse it inside the same 30-second step.
* **Enrolment is two-phase.** The secret is stored only after the user proves
  they can generate a correct code from it, so nobody locks themselves out by
  enabling MFA against a secret their app never received.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import struct
import threading
import time
from urllib.parse import quote

log = logging.getLogger("auth.mfa")

TOTP_DIGITS = int(os.environ.get("MFA_TOTP_DIGITS", "6"))
TOTP_STEP_SEC = int(os.environ.get("MFA_TOTP_STEP_SEC", "30"))
# Number of steps either side of "now" that are accepted (clock drift).
TOTP_WINDOW = int(os.environ.get("MFA_TOTP_WINDOW", "1"))
TOTP_ISSUER = os.environ.get("MFA_TOTP_ISSUER", "AranMed")

# jti-style replay guard: (user_id, code) -> expiry.
_used: dict[tuple[int, str], float] = {}
_used_lock = threading.Lock()


def generate_secret() -> str:
    """A fresh base32 secret, the format authenticator apps expect."""
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    padding = "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32.upper() + padding, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def code_at(secret_b32: str, at: float | None = None) -> str:
    """The code valid at *at* (default: now). Used by tests and enrolment."""
    ts = time.time() if at is None else at
    return _hotp(secret_b32, int(ts // TOTP_STEP_SEC))


def verify(secret_b32: str, code: str, *, user_id: int | None = None) -> bool:
    """Constant-time check across the drift window, with replay blocking."""
    if not secret_b32 or not code:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return False

    now = time.time()
    if user_id is not None:
        _sweep(now)
        with _used_lock:
            if (user_id, code) in _used:
                log.warning("mfa: replayed TOTP code rejected for user %s", user_id)
                return False

    step = int(now // TOTP_STEP_SEC)
    ok = False
    for drift in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        # No early break: compare every candidate so timing doesn't reveal
        # which step matched.
        if hmac.compare_digest(_hotp(secret_b32, step + drift), code):
            ok = True

    if ok and user_id is not None:
        with _used_lock:
            _used[(user_id, code)] = now + TOTP_STEP_SEC * (TOTP_WINDOW + 1)
    return ok


def _sweep(now: float) -> None:
    with _used_lock:
        if len(_used) < 4096:
            expired = [k for k, exp in _used.items() if exp < now]
        else:
            expired = [k for k, exp in _used.items() if exp < now] or list(_used)[:1024]
        for k in expired:
            _used.pop(k, None)


def provisioning_uri(secret_b32: str, *, account: str, issuer: str | None = None) -> str:
    """``otpauth://`` URI for an authenticator app (render as a QR code)."""
    issuer = issuer or TOTP_ISSUER
    label = quote(f"{issuer}:{account}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret_b32}"
        f"&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SEC}"
    )
