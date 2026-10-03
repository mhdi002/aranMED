"""Project the legacy JSON EHR (``patients`` table) into the relational EHR.

The dictation/agent pipeline keeps writing its simple JSON record through
``store.upsert_patient`` exactly as before. This observer turns each saved
record into MPI identity + relational rows, so everything the legacy flow
captures is visible in the clinical chart, the FHIR server and to peer
hospitals — without changing the legacy flow.

Rows produced here carry ``source_id = "legacy:<patient_id>:<kind>:<n>"`` and
are replaced as a set on every save, so editing the legacy record (removing
a medication, say) is reflected instead of accumulating stale rows.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from clinicaldb import mpi, settings
from ehr import store

log = logging.getLogger("ehr.bridge")


def _vitals(v: dict[str, Any], prefix: str, when: str) -> list[dict]:
    loinc = {"hr": ("8867-4", "Heart rate", "/min"), "rr": ("9279-1", "Respiratory rate", "/min"),
             "temp_c": ("8310-5", "Body temperature", "Cel"),
             "spo2": ("59408-5", "Oxygen saturation", "%")}
    out = []
    for key, (code, disp, unit) in loinc.items():
        if v.get(key) is not None:
            try:
                val = float(v[key])
            except (TypeError, ValueError):
                continue
            out.append({"source_id": f"{prefix}:vital:{key}", "category": "vital-signs",
                        "code_system": "http://loinc.org", "code": code, "display": disp,
                        "value_num": val, "unit": unit, "effective": when, "status": "final"})
    if v.get("bp"):
        out.append({"source_id": f"{prefix}:vital:bp", "category": "vital-signs",
                    "code_system": "http://loinc.org", "code": "85354-9",
                    "display": "Blood pressure panel", "value_text": str(v["bp"]),
                    "unit": "mm[Hg]", "effective": when, "status": "final"})
    return out


def project(record: dict, owner_user_id: int) -> dict | None:
    pid_legacy = record.get("id")
    p = record.get("patient") or {}
    if not pid_legacy or not (p.get("name") or p.get("mrn")):
        return None
    idents = []
    if p.get("mrn"):
        idents.append({"system": settings.mrn_system(), "value": str(p["mrn"]), "type": "MR",
                       "facility_oid": settings.facility_oid()})
    idents.append({"system": f"urn:aranmed:legacy-ehr:{settings.facility_oid()}",
                   "value": pid_legacy, "type": "RI"})
    demo: dict[str, Any] = {"name": p.get("name"), "sex": p.get("sex")}
    if p.get("age") and not p.get("birth_date"):
        try:
            demo["birth_date"] = str(int(time.strftime("%Y")) - int(float(p["age"])))
        except (TypeError, ValueError):
            pass
    reg = mpi.register_person(demo, idents)
    person = reg["person_id"]
    mpi.ensure_local_mrn(person, p.get("mrn"))
    prefix = f"legacy:{pid_legacy}"
    when = (record.get("encounter") or {}).get("date") or time.strftime("%Y-%m-%d")
    actor = f"user:{owner_user_id}"

    enc = record.get("encounter") or {}
    encs = []
    if enc.get("chief_complaint") or enc.get("summary") or enc.get("date"):
        encs.append({"source_id": f"{prefix}:encounter:0", "class": "AMB", "status": "finished",
                     "start_at": enc.get("date") or when, "reason": enc.get("chief_complaint"),
                     "text": enc.get("summary")})
    store.replace_source_set("encounter", person, f"{prefix}:encounter:", encs, actor=actor)
    store.replace_source_set("condition", person, f"{prefix}:condition:", [
        {"source_id": f"{prefix}:condition:{i}", "text": c.get("name"), "display": c.get("name"),
         "clinical_status": c.get("status") or "active"}
        for i, c in enumerate(record.get("problems") or []) if c.get("name")], actor=actor)
    store.replace_source_set("allergy", person, f"{prefix}:allergy:", [
        {"source_id": f"{prefix}:allergy:{i}", "text": a.get("substance"),
         "display": a.get("substance"), "reaction": a.get("reaction"), "status": "active"}
        for i, a in enumerate(record.get("allergies") or []) if a.get("substance")], actor=actor)
    store.replace_source_set("medication", person, f"{prefix}:medication:", [
        {"source_id": f"{prefix}:medication:{i}", "kind": "statement", "status": "active",
         "text": m.get("name"), "display": m.get("name"), "dose": m.get("dose"),
         "route": m.get("route"), "frequency": m.get("frequency"),
         "frequency_hours": m.get("frequency_hours"), "indication": m.get("indication"),
         "note": m.get("notes")}
        for i, m in enumerate(record.get("medications") or []) if m.get("name")], actor=actor)
    store.replace_source_set("observation", person, f"{prefix}:vital:",
                             _vitals(record.get("vitals") or {}, prefix, when), actor=actor)
    labs = []
    for i, lab in enumerate(record.get("labs") or []):
        if not lab.get("name"):
            continue
        try:
            num = float(lab.get("value"))
        except (TypeError, ValueError):
            num = None
        labs.append({"source_id": f"{prefix}:lab:{i}", "category": "laboratory",
                     "text": lab["name"], "display": lab["name"], "value_num": num,
                     "value_text": None if num is not None else lab.get("value"),
                     "unit": lab.get("unit"), "effective": lab.get("at") or when,
                     "status": "final"})
    store.replace_source_set("observation", person, f"{prefix}:lab:", labs, actor=actor)
    if record.get("notes"):
        store.replace_source_set("document", person, f"{prefix}:note:", [
            {"source_id": f"{prefix}:note:0", "doc_type": "progress-note", "title": "Clinical note",
             "content_type": "text/plain", "content": str(record["notes"]), "effective": when,
             "status": "current"}], actor=actor)
    return {"person_id": person, "outcome": reg["outcome"]}


def _hook(record: dict, owner_user_id: int) -> None:
    try:
        project(record, owner_user_id)
    except Exception:  # noqa: BLE001
        log.exception("legacy EHR projection failed for %s", record.get("id"))


def install() -> None:
    import store as legacy_store
    legacy_store.register_after_save(_hook)


def backfill() -> int:
    """Project every existing legacy record (one-off, after upgrading)."""
    import db
    import phi_crypto
    n = 0
    with db.connect() as c:
        rows = c.execute("SELECT id, owner_user_id, data FROM patients").fetchall()
    for r in rows:
        rec = phi_crypto.decrypt_json(r["data"], patient_id=r["id"], owner_user_id=r["owner_user_id"])
        if rec and project({**rec, "id": r["id"]}, r["owner_user_id"]):
            n += 1
    return n
