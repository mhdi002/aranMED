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
# Password policy
# ---------------------------------------------------------------------------
# Every threshold is configurable: a hospital's policy is theirs to set, and
# baking one in guarantees it is wrong somewhere. Defaults follow NIST SP
# 800-63B's shape — length carries most of the strength, so the minimum is
# 12 and character-class requirements are opt-in rather than mandatory.
PW_MIN_LENGTH = int(os.environ.get("PASSWORD_MIN_LENGTH", "12"))
PW_MAX_LENGTH = int(os.environ.get("PASSWORD_MAX_LENGTH", "1024"))
PW_REQUIRE_UPPER = os.environ.get("PASSWORD_REQUIRE_UPPER", "0").strip().lower() in ("1", "true", "yes", "on")
PW_REQUIRE_LOWER = os.environ.get("PASSWORD_REQUIRE_LOWER", "0").strip().lower() in ("1", "true", "yes", "on")
PW_REQUIRE_DIGIT = os.environ.get("PASSWORD_REQUIRE_DIGIT", "0").strip().lower() in ("1", "true", "yes", "on")
PW_REQUIRE_SYMBOL = os.environ.get("PASSWORD_REQUIRE_SYMBOL", "0").strip().lower() in ("1", "true", "yes", "on")
PW_BLOCKLIST_FILE = os.environ.get("PASSWORD_BLOCKLIST_FILE", "").strip()

_BUILTIN_WEAK = {
    "password", "passw0rd", "123456", "12345678", "123456789", "qwerty",
    "admin", "administrator", "letmein", "welcome", "changeme", "iloveyou",
    "abc123", "111111", "000000", "aranmed", "hospital", "clinic", "doctor",
}


def _blocklist() -> set[str]:
    words = set(_BUILTIN_WEAK)
    if PW_BLOCKLIST_FILE:
        try:
            with open(PW_BLOCKLIST_FILE, "r", encoding="utf-8", errors="ignore") as f:
                words |= {ln.strip().lower() for ln in f if ln.strip()}
        except OSError as e:
            log.warning("auth: could not read PASSWORD_BLOCKLIST_FILE (%s)", e)
    return words


def validate_password(password: str, *, username: str = "") -> None:
    """Raise ``ValueError`` describing the first unmet requirement.

    NIST's guidance is that a long password beats a short one with forced
    symbol substitutions, and that checking against known-weak values matters
    more than composition rules — so length and the blocklist are always on,
    while character-class rules default off and can be enabled per site.
    """
    if password is None:
        raise ValueError("password is required")
    if len(password) < PW_MIN_LENGTH:
        raise ValueError(f"password must be at least {PW_MIN_LENGTH} characters")
    if len(password) > PW_MAX_LENGTH:
        raise ValueError(f"password must be at most {PW_MAX_LENGTH} characters")
    if PW_REQUIRE_UPPER and not any(c.isupper() for c in password):
        raise ValueError("password must contain an uppercase letter")
    if PW_REQUIRE_LOWER and not any(c.islower() for c in password):
        raise ValueError("password must contain a lowercase letter")
    if PW_REQUIRE_DIGIT and not any(c.isdigit() for c in password):
        raise ValueError("password must contain a digit")
    if PW_REQUIRE_SYMBOL and password.isalnum():
        raise ValueError("password must contain a symbol")

    lowered = password.strip().lower()
    blocked = _blocklist()
    if lowered in blocked:
        raise ValueError("password is too common — choose something less guessable")
    # "admin12345678" and "password2026" are a blocklisted word with padding,
    # not new passwords. Strip the padding and check the core word too,
    # otherwise the blocklist is trivially defeated by appending digits.
    core = "".join(ch for ch in lowered if ch.isalpha())
    if core and core in blocked:
        raise ValueError("password is too common — choose something less guessable")
    if username and username.strip().lower() and username.strip().lower() in lowered:
        raise ValueError("password must not contain the username")


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
    # jti makes a specific token revocable. Without it, "log out everywhere"
    # can only be done by rotating the signing secret, which logs out everyone.
    payload = {"iat": int(time.time()),
               "exp": int(time.time()) + TOKEN_TTL_SEC,
               "jti": secrets.token_urlsafe(12), **payload}
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
    jti = payload.get("jti")
    if jti and is_token_revoked(jti):
        raise ValueError("token revoked")
    return payload


# ---------------------------------------------------------------------------
# Token revocation
# ---------------------------------------------------------------------------
def is_token_revoked(jti: str) -> bool:
    try:
        with db.connect() as c:
            row = c.execute("SELECT 1 FROM revoked_tokens WHERE jti=?", (jti,)).fetchone()
        return row is not None
    except Exception as e:  # noqa: BLE001
        # Fail *closed*: if the revocation list can't be consulted we cannot
        # prove the token is still valid, and a revoked-but-accepted token is
        # the worse outcome than a spurious 401.
        log.error("auth: revocation check failed (%s) — rejecting token", e)
        return True


def revoke_token(payload: dict, *, reason: str = "logout") -> bool:
    """Revoke the token described by *payload* (a decoded token). Returns
    False when the token carries no jti (issued before revocation existed).
    """
    jti = payload.get("jti")
    if not jti:
        return False
    try:
        with db.connect() as c:
            c.execute(
                "INSERT OR IGNORE INTO revoked_tokens (jti, user_id, revoked_at, expires_at, reason) "
                "VALUES (?,?,?,?,?)",
                (jti, int(payload.get("sub") or 0) or None, time.time(),
                 float(payload.get("exp") or 0), reason),
            )
        return True
    except Exception as e:  # noqa: BLE001
        log.error("auth: failed to revoke token %s: %s", jti, e)
        return False


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------
# revoked_tokens answers "was this token revoked". That is enough to enforce a
# logout but not enough for a person to *manage* their account: they cannot
# see that a session is open on a device they lost. auth_sessions records each
# issued token so it can be listed and revoked individually.
def record_session(payload: dict, *, client_ip: str | None = None,
                   user_agent: str | None = None) -> None:
    jti = payload.get("jti")
    if not jti:
        return
    try:
        with db.connect() as c:
            c.execute(
                """INSERT INTO auth_sessions
                     (jti, user_id, username, issued_at, expires_at, last_seen,
                      client_ip, user_agent, revoked_at)
                   VALUES (?,?,?,?,?,?,?,?,NULL)
                   ON CONFLICT(jti) DO UPDATE SET last_seen=excluded.last_seen""",
                (jti, int(payload.get("sub") or 0), payload.get("username"),
                 float(payload.get("iat") or time.time()),
                 float(payload.get("exp") or 0), time.time(),
                 client_ip, (user_agent or "")[:300]),
            )
    except Exception as e:  # noqa: BLE001
        log.warning("auth: could not record session %s: %s", jti, e)


def touch_session(jti: str) -> None:
    """Update last_seen so 'active sessions' reflects real use, not just issue
    time. Best-effort: a failure here must never break a request.
    """
    if not jti:
        return
    try:
        with db.connect() as c:
            c.execute("UPDATE auth_sessions SET last_seen=? WHERE jti=?",
                      (time.time(), jti))
    except Exception:  # noqa: BLE001
        pass


def list_sessions(user_id: int, *, include_expired: bool = False) -> list[dict]:
    now = time.time()
    try:
        with db.connect() as c:
            if include_expired:
                rows = c.execute(
                    "SELECT * FROM auth_sessions WHERE user_id=? ORDER BY last_seen DESC",
                    (user_id,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM auth_sessions WHERE user_id=? AND expires_at>? "
                    "AND revoked_at IS NULL ORDER BY last_seen DESC",
                    (user_id, now),
                ).fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("auth: list_sessions failed: %s", e)
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["active"] = d.get("revoked_at") is None and float(d.get("expires_at") or 0) > now
        out.append(d)
    return out


def revoke_session(jti: str, *, user_id: int, reason: str = "revoked") -> bool:
    """Revoke one session, scoped to its owner so a user can only end their own."""
    try:
        with db.connect() as c:
            row = c.execute(
                "SELECT user_id, expires_at FROM auth_sessions WHERE jti=?", (jti,)
            ).fetchone()
            if row is None or int(row["user_id"]) != int(user_id):
                return False
            now = time.time()
            c.execute("UPDATE auth_sessions SET revoked_at=? WHERE jti=?", (now, jti))
            c.execute(
                "INSERT OR IGNORE INTO revoked_tokens (jti, user_id, revoked_at, expires_at, reason) "
                "VALUES (?,?,?,?,?)",
                (jti, user_id, now, float(row["expires_at"] or 0), reason),
            )
        return True
    except Exception as e:  # noqa: BLE001
        log.error("auth: revoke_session %s failed: %s", jti, e)
        return False


def revoke_all_sessions(user_id: int, *, except_jti: str | None = None,
                        reason: str = "revoke_all") -> int:
    """End every session for a user. The classic 'sign out everywhere' after a
    password change or a suspected compromise.
    """
    now = time.time()
    count = 0
    try:
        with db.connect() as c:
            rows = c.execute(
                "SELECT jti, expires_at FROM auth_sessions WHERE user_id=? "
                "AND revoked_at IS NULL AND expires_at>?",
                (user_id, now),
            ).fetchall()
            for r in rows:
                if except_jti and r["jti"] == except_jti:
                    continue
                c.execute("UPDATE auth_sessions SET revoked_at=? WHERE jti=?", (now, r["jti"]))
                c.execute(
                    "INSERT OR IGNORE INTO revoked_tokens (jti, user_id, revoked_at, expires_at, reason) "
                    "VALUES (?,?,?,?,?)",
                    (r["jti"], user_id, now, float(r["expires_at"] or 0), reason),
                )
                count += 1
    except Exception as e:  # noqa: BLE001
        log.error("auth: revoke_all_sessions failed: %s", e)
    return count


def purge_expired_sessions() -> int:
    try:
        with db.connect() as c:
            cur = c.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (time.time(),))
            return cur.rowcount
    except Exception as e:  # noqa: BLE001
        log.warning("auth: purge_expired_sessions failed: %s", e)
        return 0


# ---------------------------------------------------------------------------
# MFA recovery codes
# ---------------------------------------------------------------------------
RECOVERY_CODE_COUNT = int(os.environ.get("MFA_RECOVERY_CODE_COUNT", "10"))


def _hash_recovery(code: str) -> str:
    """Recovery codes are high-entropy, so a fast salted hash is appropriate —
    unlike passwords, there is nothing to brute-force in a useful timeframe,
    and scrypt per code would make verification needlessly slow.
    """
    return hashlib.sha256((SECRET + "|recovery|" + code.strip().lower()).encode()).hexdigest()


def generate_recovery_codes(user_id: int) -> list[str]:
    """Replace any existing codes with a fresh set. Returned in the clear
    exactly once — only hashes are stored.
    """
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = secrets.token_hex(5)  # 10 hex chars, ~40 bits
        codes.append(f"{raw[:5]}-{raw[5:]}")
    with db.connect() as c:
        c.execute("DELETE FROM mfa_recovery_codes WHERE user_id=?", (user_id,))
        for code in codes:
            c.execute(
                "INSERT INTO mfa_recovery_codes (user_id, code_hash, created_at, used_at) "
                "VALUES (?,?,?,NULL)",
                (user_id, _hash_recovery(code), db.now()),
            )
    log.info("auth: issued %d recovery codes for user %s", len(codes), user_id)
    return codes


def consume_recovery_code(user_id: int, code: str) -> bool:
    """Verify and burn a recovery code. Single-use by construction."""
    if not code:
        return False
    h = _hash_recovery(code)
    try:
        with db.connect() as c:
            row = c.execute(
                "SELECT id FROM mfa_recovery_codes WHERE user_id=? AND code_hash=? "
                "AND used_at IS NULL",
                (user_id, h),
            ).fetchone()
            if row is None:
                return False
            c.execute("UPDATE mfa_recovery_codes SET used_at=? WHERE id=?",
                      (db.now(), row["id"]))
        log.warning("auth: MFA recovery code consumed for user %s", user_id)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("auth: consume_recovery_code failed: %s", e)
        return False


def count_recovery_codes(user_id: int) -> dict[str, int]:
    try:
        with db.connect() as c:
            total = c.execute("SELECT COUNT(*) AS n FROM mfa_recovery_codes WHERE user_id=?",
                              (user_id,)).fetchone()["n"]
            unused = c.execute("SELECT COUNT(*) AS n FROM mfa_recovery_codes "
                               "WHERE user_id=? AND used_at IS NULL",
                               (user_id,)).fetchone()["n"]
        return {"total": int(total), "remaining": int(unused)}
    except Exception:  # noqa: BLE001
        return {"total": 0, "remaining": 0}


def purge_expired_revocations() -> int:
    """Drop revocation rows whose token has expired anyway. Safe to run on a
    schedule; an expired token is rejected on its own merits.
    """
    try:
        with db.connect() as c:
            cur = c.execute("DELETE FROM revoked_tokens WHERE expires_at < ?", (time.time(),))
            return cur.rowcount
    except Exception as e:  # noqa: BLE001
        log.warning("auth: purge_expired_revocations failed: %s", e)
        return 0


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
    validate_password(password, username=username)
    if role not in ("doctor", "student", "resident", "admin", "radiologist"):
        raise ValueError("invalid role")
    with db.connect() as c:
        try:
            c.execute(
                "INSERT INTO users(username,email,password_hash,role,created_at) "
                "VALUES (?,?,?,?,?)",
                (username, email, hash_password(password), role, db.now()),
            )
        except db.INTEGRITY_ERRORS as e:
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
    user = _user_to_dict(row)
    # The bootstrap password file has served its purpose the moment its owner
    # signs in; leaving a plaintext credential on disk indefinitely is the
    # thing that turns a convenience into a liability.
    _consume_admin_credentials_file(user["username"])
    return user


def _consume_admin_credentials_file(username: str) -> None:
    path = _admin_credentials_path()
    if path is None:
        return
    try:
        if not path.is_file():
            return
        content = path.read_text(encoding="utf-8", errors="ignore")
        if f"username: {username}" not in content:
            return  # belongs to a different account; leave it alone
        path.unlink()
        log.warning("auth: deleted bootstrap credentials file %s after first "
                    "successful sign-in as %r", path, username)
    except OSError as e:
        log.warning("auth: could not remove bootstrap credentials file %s: %s", path, e)


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
    if path is None:
        # Explicitly opted out of writing a credential to disk: emit it once
        # and never persist it. Whoever is watching the boot log gets one
        # chance to capture it, which is the point.
        log.warning(
            "No ASR_AGENT_ADMIN_PASSWORD set and ADMIN_CREDENTIALS_FILE is disabled — "
            "generated password for %r (shown once, not stored anywhere): %s",
            username, secret,
        )
        return

    written = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"username: {username}\npassword: {secret}\n\n"
            "Generated on first start because ASR_AGENT_ADMIN_PASSWORD was not set.\n"
            "This file is deleted automatically on the first successful sign-in\n"
            "as this user, so read it now. Change the password afterwards.\n",
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


def _admin_credentials_path() -> Path | None:
    """Where to write the bootstrap password, or None to never write it.

    Set ``ADMIN_CREDENTIALS_FILE`` to ``none``/``off``/``-`` to keep the
    generated password off disk entirely; it is then logged once and nowhere
    else. The default writes a 0600 file that deletes itself on first
    successful sign-in.
    """
    override = os.environ.get("ADMIN_CREDENTIALS_FILE", "").strip()
    if override.lower() in ("none", "off", "false", "0", "-"):
        return None
    if override:
        return Path(override)
    return db.get_db_path().parent / "initial-admin-password.txt"


def _user_to_dict(row) -> dict:
    keys = row.keys()
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "role": row["role"],
        "created_at": row["created_at"],
        # Never expose totp_secret — this dict is returned to clients.
        "mfa_enabled": bool(row["mfa_enabled"]) if "mfa_enabled" in keys else False,
    }


# ---------------------------------------------------------------------------
# TOTP secret storage (see backend/mfa.py for the algorithm)
# ---------------------------------------------------------------------------
def get_totp_secret(user_id: int) -> str | None:
    with db.connect() as c:
        row = c.execute("SELECT totp_secret FROM users WHERE id=?", (user_id,)).fetchone()
    return (row["totp_secret"] if row else None) or None


def set_totp_secret(user_id: int, secret: str, *, enabled: bool) -> None:
    with db.connect() as c:
        c.execute("UPDATE users SET totp_secret=?, mfa_enabled=? WHERE id=?",
                  (secret, 1 if enabled else 0, user_id))


def clear_totp_secret(user_id: int) -> None:
    with db.connect() as c:
        c.execute("UPDATE users SET totp_secret=NULL, mfa_enabled=0 WHERE id=?", (user_id,))


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
    # Keep "active sessions" honest: it should reflect what is actually being
    # used, not merely what was issued.
    touch_session(payload.get("jti") or "")
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
