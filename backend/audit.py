"""Append-only audit trail.

Every authentication event, every access to patient data, and every denied
authorisation lands here. This is the record an auditor asks for and the
control HIPAA §164.312(b) ("record and examine activity in systems that
contain or use electronic protected health information") and ISO 27001
A.12.4 require — the platform claims both, so the trail has to exist and has
to be hard to silently lose.

Design constraints:

* **Append-only in practice.** Nothing in application code UPDATEs or DELETEs
  a row. Retention trimming is :func:`purge_older_than`, which an operator
  calls deliberately.
* **No PHI in the trail.** ``resource`` is an identifier (``patient:ali-reza``)
  and ``detail`` carries structural facts (field names, counts, reasons) —
  never diagnoses, names, or note text. An audit log that itself leaks PHI
  widens the breach surface instead of narrowing it.
* **Never breaks the request.** A failure to write an audit row is logged
  loudly but does not raise, unless ``AUDIT_STRICT`` is enabled — some
  regimes require the opposite trade (refuse the action if it cannot be
  recorded), so that is a configuration choice rather than a hardcoded one.
* **Survives user deletion.** ``actor_name`` is denormalised alongside
  ``actor_id`` so the trail stays readable after an account is removed.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import db

log = logging.getLogger("audit")

# Refuse the action when the audit write fails, instead of proceeding.
AUDIT_STRICT = os.environ.get("AUDIT_STRICT", "").strip().lower() in ("1", "true", "yes", "on")
# Cap on the serialised detail blob, so a pathological payload can't bloat the table.
DETAIL_MAX_CHARS = int(os.environ.get("AUDIT_DETAIL_MAX_CHARS", "2000"))


class AuditWriteError(RuntimeError):
    """Raised only when AUDIT_STRICT is on and the trail could not be written."""


def record(
    action: str,
    *,
    actor: Optional[dict] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    resource: Optional[str] = None,
    outcome: str = "allow",
    client_ip: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """Write one audit row.

    ``actor`` accepts the dict returned by ``auth.current_user`` as a
    convenience; ``actor_id``/``actor_name`` override it when the caller has
    only the raw values (e.g. a failed login, where there is no user object).
    """
    if actor:
        actor_id = actor_id if actor_id is not None else actor.get("id")
        actor_name = actor_name or actor.get("username")

    payload = None
    if detail:
        payload = db.dumps_json(detail)
        if len(payload) > DETAIL_MAX_CHARS:
            payload = payload[:DETAIL_MAX_CHARS] + '…"}'

    try:
        with db.connect() as c:
            c.execute(
                """INSERT INTO audit_log
                     (ts, actor_id, actor_name, action, resource, outcome, client_ip, detail)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (time.time(), actor_id, actor_name, action, resource,
                 outcome, client_ip, payload),
            )
    except Exception as e:  # noqa: BLE001
        log.error("audit: FAILED to record %s (%s) actor=%s resource=%s: %s",
                  action, outcome, actor_name, resource, e)
        if AUDIT_STRICT:
            raise AuditWriteError(f"audit trail unavailable: {e}") from e


def query(
    *,
    limit: int = 100,
    offset: int = 0,
    actor_id: Optional[int] = None,
    action_prefix: Optional[str] = None,
    outcome: Optional[str] = None,
    since: Optional[float] = None,
) -> list[dict]:
    """Read the trail, newest first. Filters are all optional and combine."""
    where: list[str] = []
    args: list[Any] = []
    if actor_id is not None:
        where.append("actor_id = ?")
        args.append(actor_id)
    if action_prefix:
        where.append("action LIKE ?")
        args.append(f"{action_prefix}%")
    if outcome:
        where.append("outcome = ?")
        args.append(outcome)
    if since is not None:
        where.append("ts >= ?")
        args.append(since)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    args.extend([max(1, min(limit, 1000)), max(0, offset)])
    with db.connect() as c:
        rows = c.execute(
            f"SELECT * FROM audit_log {clause} ORDER BY ts DESC LIMIT ? OFFSET ?",
            args,
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = db.loads_json(d.get("detail")) if d.get("detail") else None
        out.append(d)
    return out


def count(**kwargs: Any) -> int:
    """Total rows matching the same filters :func:`query` accepts."""
    where: list[str] = []
    args: list[Any] = []
    if kwargs.get("actor_id") is not None:
        where.append("actor_id = ?")
        args.append(kwargs["actor_id"])
    if kwargs.get("action_prefix"):
        where.append("action LIKE ?")
        args.append(f"{kwargs['action_prefix']}%")
    if kwargs.get("outcome"):
        where.append("outcome = ?")
        args.append(kwargs["outcome"])
    if kwargs.get("since") is not None:
        where.append("ts >= ?")
        args.append(kwargs["since"])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with db.connect() as c:
        return c.execute(f"SELECT COUNT(*) AS n FROM audit_log {clause}", args).fetchone()["n"]


def purge_older_than(seconds: float) -> int:
    """Delete rows older than *seconds*. The only deletion path, and it is
    deliberate: audit retention is a policy decision (HIPAA asks for six
    years), not something the application should do on its own schedule.
    Returns the number of rows removed.
    """
    cutoff = time.time() - seconds
    with db.connect() as c:
        cur = c.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
        n = cur.rowcount
    if n:
        log.warning("audit: purged %d row(s) older than %.0fs", n, seconds)
    return n
