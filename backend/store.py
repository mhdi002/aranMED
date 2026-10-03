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
import phi_crypto

log = logging.getLogger("store")


# ---------------------------------------------------------------------------
# Patients / EHR
# ---------------------------------------------------------------------------
def _slugify(name: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return base or uuid.uuid4().hex[:8]


# Observers of saved EHR records (e.g. backend/ehr/bridge.py projecting them
# into the relational EHR). A failing observer is logged, never allowed to
# fail the save the caller asked for.
_after_save_hooks: list = []


def register_after_save(fn) -> None:
    if fn not in _after_save_hooks:
        _after_save_hooks.append(fn)


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

        # `name` is a denormalised copy of data.patient.name kept for listing.
        # It is PHI in its own right, so when encryption is on it is not
        # written in the clear -- the display name comes from the decrypted
        # blob instead (see list_patients). Nothing queries this column.
        stored_name = "" if phi_crypto.enabled() else name
        stored_data = phi_crypto.encrypt_json(
            record, patient_id=pid, owner_user_id=owner_user_id
        )

        c.execute("""
            INSERT INTO patients(id, owner_user_id, name, language, data,
                                 created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name,
              language=excluded.language,
              data=excluded.data,
              updated_at=excluded.updated_at
        """, (pid, owner_user_id, stored_name, language, stored_data,
              created_at, now))
    saved = get_patient(pid, owner_user_id=owner_user_id) or record
    for hook in _after_save_hooks:
        try:
            hook(saved, owner_user_id)
        except Exception:  # noqa: BLE001
            log.exception("after-save hook failed for patient %s", pid)
    return saved


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
    rec = phi_crypto.decrypt_json(row["data"], patient_id=row["id"],
                                  owner_user_id=row["owner_user_id"]) or {}
    rec.update({"id": row["id"], "language": row["language"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"]})
    return rec


def list_patients(*, owner_user_id: int) -> list[dict]:
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, owner_user_id, name, language, updated_at, data "
            "FROM patients WHERE owner_user_id=? ORDER BY updated_at DESC",
            (owner_user_id,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            d = phi_crypto.decrypt_json(r["data"], patient_id=r["id"],
                                        owner_user_id=r["owner_user_id"]) or {}
        except ValueError as e:
            # One unreadable record must not blank the whole list; surface it
            # as an explicitly broken entry so it is visible rather than
            # quietly missing from a clinician's patient list.
            log.error("store: cannot read record %s: %s", r["id"], e)
            out.append({"id": r["id"], "name": None, "language": r["language"],
                        "updated_at": r["updated_at"], "medications": 0,
                        "error": "unreadable"})
            continue
        meds = d.get("medications") or []
        # Prefer the decrypted name; fall back to the legacy plaintext column
        # for rows written before encryption was enabled.
        name = (d.get("patient") or {}).get("name") or r["name"] or None
        out.append({
            "id": r["id"], "name": name, "language": r["language"],
            "updated_at": r["updated_at"], "medications": len(meds),
        })
    return out


def find_patient_by_mrn(mrn: str, *, owner_user_id: int) -> str | None:
    """Return the id of the caller's patient whose stored patient.mrn matches
    (case/whitespace-insensitive), or None. Queries full records — MRN lives
    inside the JSON ``data`` blob, not a summary column list_patients()
    returns — so this can't be answered from that summary alone.
    """
    target = (mrn or "").strip().lower()
    if not target:
        return None
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, owner_user_id, data FROM patients WHERE owner_user_id=?",
            (owner_user_id,),
        ).fetchall()
    for r in rows:
        try:
            d = phi_crypto.decrypt_json(r["data"], patient_id=r["id"],
                                        owner_user_id=r["owner_user_id"]) or {}
        except ValueError:
            continue  # unreadable row can't match; logged by list_patients
        existing = ((d.get("patient") or {}).get("mrn") or "").strip().lower()
        if existing and existing == target:
            return r["id"]
    return None


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
