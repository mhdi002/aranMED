"""PostgreSQL support behind the SQLite-shaped API the rest of the code uses.

Every module here calls ``db.connect()`` and writes ``?``-style SQL against a
``sqlite3.Connection``. Rewriting 36 call sites to be dialect-aware would
spread database detail through modules that have no business knowing it, so
instead this provides a thin connection wrapper that speaks the same surface
(``execute`` / ``executescript`` / ``fetchone`` / ``fetchall`` / ``rowcount``
/ context manager) on top of psycopg.

Two properties matter more than elegance here:

* **The SQLite path is untouched.** ``db.connect()`` returns the raw
  ``sqlite3.Connection`` exactly as before when no ``DATABASE_URL`` is set,
  so the default deployment carries none of this code and none of its risk.
* **Row access is already compatible.** ``sqlite3.Row`` supports
  ``row["col"]`` and ``row.keys()``; psycopg's ``dict_row`` returns a dict,
  which supports both. Nothing calling code does to a row needs to change.

Why Postgres at all: SQLite serialises writers. WAL and per-statement
transactions removed the blocking reads and the lost-update races, but a
single writer is a ceiling, and a clinical deployment with many concurrent
dictations is exactly the shape that hits it.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional

log = logging.getLogger("db.dialect")

# ---------------------------------------------------------------------------
# Placeholder translation
# ---------------------------------------------------------------------------
# `?` -> `%s`, skipping anything inside a string literal or a quoted
# identifier. Our SQL contains no literal `?` today, but translating blindly
# would corrupt the first query that does, and that failure would be subtle.
_QUOTE_OPENERS = {"'": "'", '"': '"'}


def translate_placeholders(sql: str) -> str:
    out: list[str] = []
    quote: Optional[str] = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                # Doubled quote is an escape, not a terminator.
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in _QUOTE_OPENERS:
            quote = _QUOTE_OPENERS[ch]
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            # psycopg treats % as its own escape; double it so a literal
            # percent (e.g. a LIKE pattern) survives.
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Statement rewriting for constructs SQLite and Postgres spell differently
# ---------------------------------------------------------------------------
_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)
_PRAGMA = re.compile(r"^\s*PRAGMA\b", re.IGNORECASE)
_BEGIN_IMMEDIATE = re.compile(r"^\s*BEGIN\s+IMMEDIATE\s*;?\s*$", re.IGNORECASE)


def rewrite(sql: str) -> Optional[str]:
    """Adapt one statement to Postgres, or return None to skip it entirely."""
    if _PRAGMA.match(sql):
        # PRAGMAs are SQLite storage tuning (journal_mode, busy_timeout).
        # Postgres has no equivalent knobs to set per connection here.
        return None
    if _BEGIN_IMMEDIATE.match(sql):
        # SQLite takes the write lock up front; Postgres achieves the same
        # effect with row-level locking inside a normal transaction.
        return "BEGIN"
    if _INSERT_OR_IGNORE.search(sql):
        sql = _INSERT_OR_IGNORE.sub("INSERT INTO", sql)
        if "ON CONFLICT" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return sql


class _Cursor:
    """Mimics the parts of sqlite3.Cursor the codebase actually uses."""

    __slots__ = ("_cur", "_skipped")

    def __init__(self, cur: Any, skipped: bool = False) -> None:
        self._cur = cur
        self._skipped = skipped

    def fetchone(self) -> Any:
        if self._skipped:
            return None
        return self._cur.fetchone()

    def fetchall(self) -> list:
        if self._skipped:
            return []
        return self._cur.fetchall()

    def __iter__(self):
        if self._skipped:
            return iter(())
        return iter(self._cur)

    @property
    def rowcount(self) -> int:
        return 0 if self._skipped else self._cur.rowcount

    @property
    def lastrowid(self) -> Any:
        return None if self._skipped else getattr(self._cur, "lastrowid", None)


class PgConnection:
    """SQLite-shaped facade over a psycopg connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    # -- sqlite3.Connection surface ----------------------------------------
    def execute(self, sql: str, params: Iterable = ()) -> _Cursor:
        rewritten = rewrite(sql)
        if rewritten is None:
            return _Cursor(None, skipped=True)
        cur = self._conn.cursor()
        cur.execute(translate_placeholders(rewritten), tuple(params))
        return _Cursor(cur)

    def executescript(self, script: str) -> None:
        # Split on semicolons that terminate a statement. Our schema has no
        # semicolons inside literals or function bodies, so a simple split is
        # correct here and much easier to reason about than a parser.
        for stmt in script.split(";"):
            if stmt.strip():
                self.execute(stmt)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    # -- context manager ----------------------------------------------------
    # sqlite3 with isolation_level=None is autocommit and `with conn:` is a
    # no-op wrapper the codebase relies on. psycopg connections are opened
    # with autocommit=True to match, so this only has to close nothing and
    # let exceptions propagate.
    def __enter__(self) -> "PgConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


# ---------------------------------------------------------------------------
# Schema, in Postgres spelling
# ---------------------------------------------------------------------------
# Kept beside the SQLite schema in db.py rather than generated from it: the
# differences are few and explicit, and a translator that silently produced
# the wrong column type would be worse than two readable definitions.
PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            BIGSERIAL PRIMARY KEY,
  username      TEXT    NOT NULL UNIQUE,
  email         TEXT    UNIQUE,
  password_hash TEXT    NOT NULL,
  role          TEXT    NOT NULL DEFAULT 'doctor',
  created_at    DOUBLE PRECISION NOT NULL,
  totp_secret   TEXT,
  mfa_enabled   INTEGER NOT NULL DEFAULT 0,
  password_changed_at DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS patients (
  id            TEXT    PRIMARY KEY,
  owner_user_id BIGINT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name          TEXT,
  language      TEXT    NOT NULL DEFAULT 'en',
  data          TEXT    NOT NULL,
  created_at    DOUBLE PRECISION NOT NULL,
  updated_at    DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_patients_owner ON patients(owner_user_id);

CREATE TABLE IF NOT EXISTS alerts (
  id           BIGSERIAL PRIMARY KEY,
  patient_id   TEXT    NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
  channel      TEXT    NOT NULL,
  recipient    TEXT    NOT NULL,
  body         TEXT    NOT NULL,
  sent_at      DOUBLE PRECISION NOT NULL,
  dry_run      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alerts_patient ON alerts(patient_id);

CREATE TABLE IF NOT EXISTS quizzes (
  id            BIGSERIAL PRIMARY KEY,
  owner_user_id BIGINT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind          TEXT    NOT NULL,
  topic         TEXT    NOT NULL,
  language      TEXT    NOT NULL DEFAULT 'en',
  data          TEXT    NOT NULL,
  created_at    DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_quizzes_owner ON quizzes(owner_user_id);

CREATE TABLE IF NOT EXISTS audit_log (
  id           BIGSERIAL PRIMARY KEY,
  ts           DOUBLE PRECISION NOT NULL,
  actor_id     BIGINT,
  actor_name   TEXT,
  action       TEXT    NOT NULL,
  resource     TEXT,
  outcome      TEXT    NOT NULL,
  client_ip    TEXT,
  detail       TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS ix_audit_actor ON audit_log(actor_id, ts);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_log(action, ts);

CREATE TABLE IF NOT EXISTS revoked_tokens (
  jti         TEXT    PRIMARY KEY,
  user_id     BIGINT,
  revoked_at  DOUBLE PRECISION NOT NULL,
  expires_at  DOUBLE PRECISION NOT NULL,
  reason      TEXT
);
CREATE INDEX IF NOT EXISTS ix_revoked_expires ON revoked_tokens(expires_at);

CREATE TABLE IF NOT EXISTS agent_sessions (
  session_id  TEXT    PRIMARY KEY,
  messages    TEXT    NOT NULL,
  summary     TEXT    NOT NULL DEFAULT '',
  last_used   DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_agent_sessions_last_used ON agent_sessions(last_used);

CREATE TABLE IF NOT EXISTS login_throttle (
  key          TEXT    PRIMARY KEY,
  attempts     TEXT    NOT NULL DEFAULT '[]',
  locked_until DOUBLE PRECISION NOT NULL DEFAULT 0,
  updated_at   DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
  jti         TEXT    PRIMARY KEY,
  user_id     BIGINT  NOT NULL,
  username    TEXT,
  issued_at   DOUBLE PRECISION NOT NULL,
  expires_at  DOUBLE PRECISION NOT NULL,
  last_seen   DOUBLE PRECISION NOT NULL,
  client_ip   TEXT,
  user_agent  TEXT,
  revoked_at  DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS ix_auth_sessions_user ON auth_sessions(user_id, expires_at);

CREATE TABLE IF NOT EXISTS mfa_recovery_codes (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  code_hash   TEXT    NOT NULL,
  created_at  DOUBLE PRECISION NOT NULL,
  used_at     DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS ix_recovery_user ON mfa_recovery_codes(user_id);
"""


def connect(dsn: str) -> PgConnection:
    import psycopg  # noqa: PLC0415
    from psycopg.rows import dict_row  # noqa: PLC0415

    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    return PgConnection(conn)
