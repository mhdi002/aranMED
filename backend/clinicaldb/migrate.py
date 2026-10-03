"""Versioned, portable migrations for the clinical feature packages.

The core tables in :mod:`db` predate this and keep their own two-spelling
schema. Everything added for PACS / EHR / interop is written once, in SQL
both SQLite and Postgres accept, so a hospital running either backend has
the *same* schema — that sameness is what lets one hospital's records be
read, linked and imported by another.

Portable spelling rules (enforced by review, exercised by the suite running
on both backends):

* Primary keys are ``TEXT`` (UUIDs or globally unique standard identifiers
  such as DICOM UIDs) — never auto-increment integers, which collide the
  moment two hospitals' rows meet.
* Timestamps are ``DOUBLE PRECISION`` epoch seconds. (SQLite gives it REAL
  affinity, Postgres a float8; plain ``REAL`` would be float4 on Postgres
  and lose seconds.)
* Booleans are ``INTEGER`` 0/1, structured payloads are ``TEXT`` JSON.
* Every statement is idempotent (``IF NOT EXISTS``), so re-running a
  migration — after a restore, or on a database re-initialised by tests —
  is harmless. Column additions go through :func:`add_column`.
* No semicolons inside statements or comments: the Postgres facade splits
  scripts on ``;``.

Each package registers ``(version, sql_or_callable)`` steps under its own
module name; applied versions are recorded in ``schema_migrations`` and
reported by ``GET /api/interop/capabilities`` so peers can confirm they
speak the same schema before exchanging data.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Union

import db

log = logging.getLogger("clinicaldb.migrate")

Step = Union[str, Callable[[object], None]]

_registry: dict[str, list[tuple[int, Step]]] = {}
_lock = threading.Lock()

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  module      TEXT NOT NULL,
  version     INTEGER NOT NULL,
  applied_at  DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (module, version)
)
"""


def add_column(conn, table: str, column: str, ddl: str) -> None:
    """Add *column* to *table* if it is missing, on either backend."""
    if db.is_postgres():
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")
        return
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if existing and column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _apply(module: str) -> None:
    steps = sorted(_registry.get(module, []), key=lambda s: s[0])
    with _lock, db.connect() as c:
        c.executescript(_BOOTSTRAP)
        done = {int(r["version"]) for r in c.execute(
            "SELECT version FROM schema_migrations WHERE module=?", (module,))}
        for version, step in steps:
            if version in done:
                continue
            if callable(step):
                step(c)
            else:
                c.executescript(step)
            c.execute(
                "INSERT OR IGNORE INTO schema_migrations(module, version, applied_at) "
                "VALUES (?,?,?)", (module, version, db.now()))
            log.info("migrate: %s v%d applied (%s)", module, version, db.backend_name())


def register(module: str, steps: list[tuple[int, Step]]) -> None:
    """Register *module*'s migration steps and apply any not yet applied."""
    _registry[module] = list(steps)
    db.register_schema_hook(lambda m=module: _apply(m))


def applied_versions() -> dict[str, int]:
    """Highest applied version per module, e.g. ``{"pacs": 2, "mpi": 1}``."""
    with db.connect() as c:
        c.executescript(_BOOTSTRAP)
        rows = c.execute(
            "SELECT module, MAX(version) AS v FROM schema_migrations GROUP BY module"
        ).fetchall()
    return {r["module"]: int(r["v"]) for r in rows}


def expected_versions() -> dict[str, int]:
    """Versions this build of the code knows about (what peers must match)."""
    return {m: max(v for v, _ in steps) for m, steps in _registry.items() if steps}
