"""Database-backed CRUD helpers for patient EHR records, alerts and quizzes.

These wrap the raw SQL in :mod:`db` so tools and HTTP endpoints don't have
to know about SQLite specifics.  Records are scoped per user so a doctor
only sees their own patients.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any

import db

log = logging.getLogger("store")


# ---------------------------------------------------------------------------
# Patients / EHR
# ---------------------------------------------------------------------------
def _slugify(name: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return base or uuid.uuid4().hex[:8]


def upsert_patient(*, owner_user_id: int, data: dict,
                   patient_id: str | None = None,
                   language: str = "en") -> dict:
    """Insert or update an EHR record.  Returns the stored record + id."""
    name = (data.get("patient") or {}).get("name") or ""
    pid = patient_id or _slugify(name) or uuid.uuid4().hex[:8]
    # Make sure the id is unique per owner: if another owner already has it,
    # uniquify with a short suffix.
    with db.connect() as c:
        existing = c.execute(
            "SELECT owner_user_id FROM patients WHERE id=?", (pid,)
        ).fetchone()
        if existing and existing["owner_user_id"] != owner_user_id:
            pid = f"{pid}-{uuid.uuid4().hex[:4]}"

        now = db.now()
        record = {"id": pid, "language": language, **data}
        row = c.execute("SELECT created_at FROM patients WHERE id=?",
                        (pid,)).fetchone()
        created_at = row["created_at"] if row else now

        c.execute("""
            INSERT INTO patients(id, owner_user_id, name, language, data,
                                 created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name,
              language=excluded.language,
              data=excluded.data,
              updated_at=excluded.updated_at
        """, (pid, owner_user_id, name, language, db.dumps_json(record),
              created_at, now))
    return get_patient(pid, owner_user_id=owner_user_id) or record


def get_patient(patient_id: str, *, owner_user_id: int | None = None) -> dict | None:
    with db.connect() as c:
        if owner_user_id is None:
            row = c.execute("SELECT * FROM patients WHERE id=?",
                            (patient_id,)).fetchone()
        else:
            row = c.execute(
                "SELECT * FROM patients WHERE id=? AND owner_user_id=?",
                (patient_id, owner_user_id),
            ).fetchone()
    if row is None:
        return None
    rec = db.loads_json(row["data"]) or {}
    rec.update({"id": row["id"], "language": row["language"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"]})
    return rec


def list_patients(*, owner_user_id: int) -> list[dict]:
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, name, language, updated_at, data "
            "FROM patients WHERE owner_user_id=? ORDER BY updated_at DESC",
            (owner_user_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = db.loads_json(r["data"]) or {}
        meds = d.get("medications") or []
        out.append({
            "id": r["id"], "name": r["name"], "language": r["language"],
            "updated_at": r["updated_at"], "medications": len(meds),
        })
    return out


def delete_patient(patient_id: str, *, owner_user_id: int) -> bool:
    with db.connect() as c:
        cur = c.execute("DELETE FROM patients WHERE id=? AND owner_user_id=?",
                        (patient_id, owner_user_id))
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
def log_alert(*, patient_id: str, channel: str, recipient: str, body: str,
              dry_run: bool) -> int:
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO alerts(patient_id, channel, recipient, body, "
            "                   sent_at, dry_run) VALUES(?,?,?,?,?,?)",
            (patient_id, channel, recipient, body, db.now(), int(dry_run)),
        )
        return cur.lastrowid or 0


def list_alerts(*, patient_id: str | None = None,
                owner_user_id: int | None = None, limit: int = 50) -> list[dict]:
    q = ("SELECT a.* FROM alerts a "
         "JOIN patients p ON p.id = a.patient_id WHERE 1=1")
    args: list[Any] = []
    if patient_id is not None:
        q += " AND a.patient_id=?"
        args.append(patient_id)
    if owner_user_id is not None:
        q += " AND p.owner_user_id=?"
        args.append(owner_user_id)
    q += " ORDER BY a.sent_at DESC LIMIT ?"
    args.append(limit)
    with db.connect() as c:
        rows = c.execute(q, args).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Quizzes / education content
# ---------------------------------------------------------------------------
def save_quiz(*, owner_user_id: int, kind: str, topic: str,
              language: str, data: dict) -> int:
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO quizzes(owner_user_id, kind, topic, language, "
            "                    data, created_at) VALUES(?,?,?,?,?,?)",
            (owner_user_id, kind, topic, language,
             db.dumps_json(data), db.now()),
        )
        return cur.lastrowid or 0


def list_quizzes(*, owner_user_id: int, limit: int = 50) -> list[dict]:
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, kind, topic, language, created_at "
            "FROM quizzes WHERE owner_user_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (owner_user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_quiz(quiz_id: int, *, owner_user_id: int) -> dict | None:
    with db.connect() as c:
        row = c.execute(
            "SELECT * FROM quizzes WHERE id=? AND owner_user_id=?",
            (quiz_id, owner_user_id),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["data"] = db.loads_json(d["data"])
    return d
