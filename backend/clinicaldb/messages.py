"""Interop message log — every HL7/FHIR/DICOM/EMS exchange, in and out.

Operators need to answer "did hospital B ever receive that ADT?" without
grepping logs, and an auditor needs the same trail. Payloads are stored as
received (they are PHI — the table is admin-only through the API).
"""
from __future__ import annotations

from typing import Any, Optional

import db
from clinicaldb import schema  # noqa: F401
from clinicaldb.util import new_id, row

_MAX_PAYLOAD = 200_000


def log(*, direction: str, protocol: str, status: str, message_type: Optional[str] = None,
        peer: Optional[str] = None, control_id: Optional[str] = None,
        person_id: Optional[str] = None, payload: Optional[str] = None,
        response: Optional[str] = None, error: Optional[str] = None) -> str:
    mid = new_id()
    with db.connect() as c:
        c.execute(
            "INSERT INTO interop_messages(id, direction, protocol, message_type, peer, "
            "control_id, status, person_id, payload, response, error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, direction, protocol, message_type, peer, control_id, status, person_id,
             (payload or "")[:_MAX_PAYLOAD] or None, (response or "")[:_MAX_PAYLOAD] or None,
             error, db.now()))
    return mid


def query(*, protocol: Optional[str] = None, direction: Optional[str] = None,
          status: Optional[str] = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    clauses, params = [], []
    for col, val in (("protocol", protocol), ("direction", direction), ("status", status)):
        if val:
            clauses.append(f"{col}=?")
            params.append(val)
    sql = "SELECT * FROM interop_messages"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    with db.connect() as c:
        return [row(r) for r in c.execute(sql, (*params, limit, offset)).fetchall()]
