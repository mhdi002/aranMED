"""SQLite database layer.

A tiny, dependency-free persistence layer on top of stdlib :mod:`sqlite3`.
We deliberately avoid an ORM to keep the install footprint small and the
test setup obvious.

Tables
------
* ``users``      — registered humans (id, username, email, password_hash,
                   role: 'doctor'|'radiologist'|'student'|'resident'|'admin', created_at)
* ``patients``   — EHR documents (id, owner_user_id, name, language,
                   data JSON blob, created_at, updated_at)
* ``alerts``     — alert history (id, patient_id, channel, recipient,
                   body, sent_at, dry_run)
* ``quizzes``    — saved education content (id, owner_user_id, kind, topic,
                   language, data JSON, created_at)
* ``agent_sessions`` — durable backing store for backend/memory.py's
                   conversation cache (session_id, messages JSON, summary,
                   last_used), so a restart/rescale doesn't lose history.
* ``login_throttle`` — shared brute-force counters (key, attempts JSON,
                   locked_until) for backend/auth.py, so the lockout budget
                   is global rather than per-replica.

The DB file lives at ``backend/data/app.db`` by default; tests override it
via :func:`set_db_path`.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("db")

_DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "app.db"
_db_path: Path = _DEFAULT_PATH
_lock = threading.Lock()

# --- Concurrency tuning -----------------------------------------------------
# The default rollback journal takes an exclusive lock for the whole write and
# blocks readers; with several backend replicas sharing backend/data/app.db
# (a bind mount in docker-compose.yml) that serialises far more than it needs
# to. WAL lets readers keep reading during a write, and busy_timeout makes a
# concurrent writer wait for the lock instead of failing instantly with
# "database is locked". Both are env-tunable — see docs/core/CONFIGURATION.md.
DB_JOURNAL_MODE = os.getenv("DB_JOURNAL_MODE", "WAL")
DB_SYNCHRONOUS = os.getenv("DB_SYNCHRONOUS", "NORMAL")
DB_BUSY_TIMEOUT_MS = int(os.getenv("DB_BUSY_TIMEOUT_MS", "5000"))


def set_db_path(p: Path | str) -> None:
    """Override the SQLite file (tests use this with a tmp_path)."""
    global _db_path
    _db_path = Path(p)
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    _init_schema()


def get_db_path() -> Path:
    return _db_path


# --- Backend selection ------------------------------------------------------
# Set DATABASE_URL to a postgresql:// DSN to use Postgres, which removes
# SQLite's single-writer ceiling. Unset (the default) keeps the SQLite path
# exactly as it was — connect() returns the raw sqlite3.Connection and none of
# the dialect layer is involved, so the default deployment carries no new risk.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def is_postgres() -> bool:
    return DATABASE_URL.startswith(("postgres://", "postgresql://"))


def backend_name() -> str:
    return "postgres" if is_postgres() else "sqlite"


def connect():
    """A connection speaking the sqlite3 API surface, on either backend."""
    if is_postgres():
        import dialect  # noqa: PLC0415
        return dialect.connect(DATABASE_URL)

    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # busy_timeout is per-connection and must be set every time; journal_mode
    # is persistent in the database file, so _init_schema sets it once.
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
    conn.execute(f"PRAGMA synchronous = {DB_SYNCHRONOUS}")
    return conn


def begin_immediate(conn) -> None:
    """Start a transaction that takes the write lock up front.

    SQLite spells this ``BEGIN IMMEDIATE``; Postgres reaches the same place
    with a plain ``BEGIN`` plus row locking. Callers that need a serialised
    read-modify-write use this instead of writing either spelling directly.
    """
    conn.execute("BEGIN IMMEDIATE" if not is_postgres() else "BEGIN")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT    NOT NULL UNIQUE,
  email         TEXT    UNIQUE,
  password_hash TEXT    NOT NULL,
  role          TEXT    NOT NULL DEFAULT 'doctor',
  created_at    REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS patients (
  id            TEXT    PRIMARY KEY,
  owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name          TEXT,
  language      TEXT    NOT NULL DEFAULT 'en',
  data          TEXT    NOT NULL,
  created_at    REAL    NOT NULL,
  updated_at    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_patients_owner ON patients(owner_user_id);

CREATE TABLE IF NOT EXISTS alerts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  patient_id   TEXT    NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
  channel      TEXT    NOT NULL,
  recipient    TEXT    NOT NULL,
  body         TEXT    NOT NULL,
  sent_at      REAL    NOT NULL,
  dry_run      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alerts_patient ON alerts(patient_id);

CREATE TABLE IF NOT EXISTS quizzes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind          TEXT    NOT NULL,
  topic         TEXT    NOT NULL,
  language      TEXT    NOT NULL DEFAULT 'en',
  data          TEXT    NOT NULL,
  created_at    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_quizzes_owner ON quizzes(owner_user_id);

-- Append-only record of security- and PHI-relevant actions. Required by
-- HIPAA §164.312(b) / ISO 27001 A.12.4 and by the platform's own governance
-- claims. Never UPDATEd or DELETEd by application code; retention trimming is
-- an explicit operator action (see backend/audit.py:purge_older_than).
CREATE TABLE IF NOT EXISTS audit_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           REAL    NOT NULL,
  actor_id     INTEGER,                   -- NULL for anonymous/failed auth
  actor_name   TEXT,                      -- denormalised: survives user deletion
  action       TEXT    NOT NULL,          -- e.g. ehr.read, auth.login
  resource     TEXT,                      -- e.g. patient:ali-reza
  outcome      TEXT    NOT NULL,          -- allow | deny | error
  client_ip    TEXT,
  detail       TEXT                       -- JSON, no PHI values
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS ix_audit_actor ON audit_log(actor_id, ts);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_log(action, ts);

-- Token revocation. Tokens carry a jti; logout/revoke inserts it here and
-- current_user() rejects any token whose jti is present. Rows are prunable
-- once past the token TTL, since an expired token is rejected anyway.
CREATE TABLE IF NOT EXISTS revoked_tokens (
  jti         TEXT    PRIMARY KEY,
  user_id     INTEGER,
  revoked_at  REAL    NOT NULL,
  expires_at  REAL    NOT NULL,
  reason      TEXT
);
CREATE INDEX IF NOT EXISTS ix_revoked_expires ON revoked_tokens(expires_at);

-- Active login sessions. revoked_tokens answers "was this token revoked";
-- this answers "which sessions does this user have open", which is what a
-- person needs to see and revoke a device they no longer trust.
CREATE TABLE IF NOT EXISTS auth_sessions (
  jti         TEXT    PRIMARY KEY,
  user_id     INTEGER NOT NULL,
  username    TEXT,
  issued_at   REAL    NOT NULL,
  expires_at  REAL    NOT NULL,
  last_seen   REAL    NOT NULL,
  client_ip   TEXT,
  user_agent  TEXT,
  revoked_at  REAL
);
CREATE INDEX IF NOT EXISTS ix_auth_sessions_user ON auth_sessions(user_id, expires_at);

-- Single-use MFA recovery codes, stored hashed. Without these, losing the
-- authenticator app means an admin has to clear totp_secret by hand.
CREATE TABLE IF NOT EXISTS mfa_recovery_codes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  code_hash   TEXT    NOT NULL,
  created_at  REAL    NOT NULL,
  used_at     REAL
);
CREATE INDEX IF NOT EXISTS ix_recovery_user ON mfa_recovery_codes(user_id);

CREATE TABLE IF NOT EXISTS agent_sessions (
  session_id  TEXT    PRIMARY KEY,
  messages    TEXT    NOT NULL,
  summary     TEXT    NOT NULL DEFAULT '',
  last_used   REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_agent_sessions_last_used ON agent_sessions(last_used);

CREATE TABLE IF NOT EXISTS login_throttle (
  key           TEXT    PRIMARY KEY,
  attempts      TEXT    NOT NULL DEFAULT '[]',
  locked_until  REAL    NOT NULL DEFAULT 0,
  updated_at    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_login_throttle_updated ON login_throttle(updated_at);
"""


# Columns added after the initial release. CREATE TABLE IF NOT EXISTS does not
# add them to a database that already has the table, so they are applied
# idempotently here. Append-only list of (table, column, DDL).
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("users", "totp_secret", "TEXT"),
    ("users", "mfa_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "password_changed_at", "REAL"),
]


def _migrate(c: sqlite3.Connection) -> None:
    for table, column, ddl in _ADDED_COLUMNS:
        existing = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table doesn't exist yet; _SCHEMA created it above
        if column not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            log.info("db: migrated — added column %s.%s", table, column)


def _init_schema() -> None:
    if is_postgres():
        import dialect  # noqa: PLC0415
        with _lock, connect() as c:
            c.executescript(dialect.PG_SCHEMA)
        log.info("db: schema ready on postgres")
        return

    with _lock, connect() as c:
        # journal_mode persists in the file itself, so this only has to run
        # once per database, not per connection. Guarded because setting it
        # can fail on exotic filesystems (some network mounts) -- falling back
        # to the default journal is slower under concurrency but still correct.
        try:
            mode = c.execute(f"PRAGMA journal_mode = {DB_JOURNAL_MODE}").fetchone()
            active = mode[0] if mode else "?"
            if str(active).lower() != DB_JOURNAL_MODE.lower():
                log.warning("db: requested journal_mode=%s but database reports %s",
                            DB_JOURNAL_MODE, active)
        except sqlite3.Error as e:
            log.warning("db: could not set journal_mode=%s (%s)", DB_JOURNAL_MODE, e)
        c.executescript(_SCHEMA)
        _migrate(c)


# Initialise default DB at import time so casual scripts work.
_init_schema()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def now() -> float:
    return time.time()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def loads_json(s: str | None) -> Any:
    if s is None:
        return None
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return None


def dumps_json(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))
