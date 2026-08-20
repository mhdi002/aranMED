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


def connect() -> sqlite3.Connection:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # busy_timeout is per-connection and must be set every time; journal_mode
    # is persistent in the database file, so _init_schema sets it once.
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
    conn.execute(f"PRAGMA synchronous = {DB_SYNCHRONOUS}")
    return conn


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


def _init_schema() -> None:
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
