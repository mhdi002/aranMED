"""Reports linked to studies (radiologist-written, agent-drafted, or HL7 ORU).

Status ladder: ``draft`` (agent or in-progress) -> ``preliminary`` ->
``final`` -> ``amended``. Finalising a report moves the study to
``reported``; the text is what the EHR's DiagnosticReport and the FHIR
server expose for the study.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

import db
from clinicaldb.util import jdump, jload, new_id, row
from pacs import index, schema  # noqa: F401

STATUSES = ("draft", "preliminary", "final", "amended", "cancelled")
_hooks: list[Callable[[dict], None]] = []


def register_hook(fn: Callable[[dict], None]) -> None:
    """``fn(report)`` after a report is saved (EHR projection, notifications)."""
    if fn not in _hooks:
        _hooks.append(fn)


def _out(r) -> Optional[dict]:
    d = row(r)
    if d:
        d["data"] = jload(d.get("data"), {})
    return d


def list_for_study(study_uid: str) -> list[dict]:
    with db.connect() as c:
        return [_out(r) for r in c.execute(
            "SELECT * FROM pacs_study_reports WHERE study_uid=? ORDER BY created_at DESC",
            (study_uid,)).fetchall()]


def get(report_id: str) -> Optional[dict]:
    with db.connect() as c:
        return _out(c.execute("SELECT * FROM pacs_study_reports WHERE id=?",
                              (report_id,)).fetchone())


def save(study_uid: str, *, text: str, status: str = "draft", source: str = "radiologist",
         author: Optional[str] = None, data: Optional[dict[str, Any]] = None,
         report_id: Optional[str] = None) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    study = index.get_study(study_uid)
    if not study:
        raise ValueError("unknown study")
    now = db.now()
    with db.connect() as c:
        existing = c.execute("SELECT status FROM pacs_study_reports WHERE id=?",
                             (report_id,)).fetchone() if report_id else None
        if existing:
            if existing["status"] == "final" and status not in ("amended", "final"):
                raise ValueError("a final report can only be amended")
            c.execute("UPDATE pacs_study_reports SET text=?, status=?, author=COALESCE(?, author), "
                      "data=?, updated_at=? WHERE id=?",
                      (text, status, author, jdump(data or {}), now, report_id))
        else:
            report_id = new_id()
            c.execute("INSERT INTO pacs_study_reports(id, study_uid, person_id, status, source, "
                      "author, text, data, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (report_id, study_uid, study["_ext"]["person_id"], status, source, author,
                       text, jdump(data or {}), now, now))
    if status in ("final", "amended"):
        index.set_study_status(study_uid, "reported")
    elif status == "preliminary":
        index.set_study_status(study_uid, "read")
    rep = get(report_id)
    for h in _hooks:
        h(rep)
    return rep  # type: ignore[return-value]
