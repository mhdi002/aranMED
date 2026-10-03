"""Read-side helpers for inter-hospital transfers (state lives in ehr_transfers;
the workflow itself is backend/interop/transfers.py)."""
from __future__ import annotations

import db
from clinicaldb.util import jload, row


def decode(r) -> dict:
    d = row(r)
    d["history"] = jload(d.get("history"), [])
    d["package_manifest"] = jload(d.get("package_manifest"), {})
    return d


def for_person(person_id: str) -> list[dict]:
    from clinicaldb import facilities
    with db.connect() as c:
        out = [decode(r) for r in c.execute(
            "SELECT * FROM ehr_transfers WHERE person_id=? ORDER BY created_at DESC",
            (person_id,)).fetchall()]
    for t in out:
        for k in ("from_facility", "to_facility"):
            f = facilities.get_by_oid(t.get(k) or "")
            t[f"{k}_name"] = f["name"] if f else t.get(k)
    return out
