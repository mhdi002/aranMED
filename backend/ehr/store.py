"""Generic, versioned CRUD over the relational EHR tables.

One code path for every resource type keeps the semantics uniform —
versioning, history snapshots, soft delete, import de-duplication and the
MPI merge repointing all behave the same whether the row is a lab result or
an allergy.

Document ``content`` is encrypted at rest with :mod:`phi_crypto`, bound to
the person id, because free-text notes and discharge summaries are the most
sensitive thing the EHR holds. Structured columns stay queryable; protect
them with database/volume encryption (see docs/core/SECURITY.md).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Iterable, Optional

import db
import phi_crypto
from clinicaldb import mpi, settings
from clinicaldb.util import jdump, jload, new_id, row
from ehr import schema  # noqa: F401  (registers tables)

log = logging.getLogger("ehr.store")

_COMMON = ("person_id", "facility_oid", "source_facility", "source_id", "encounter_id",
           "status", "code_system", "code", "display", "text", "note", "data", "created_by")

RESOURCES: dict[str, dict[str, Any]] = {
    "encounter": {"table": "ehr_encounters", "cols": (
        "class", "type_text", "reason", "start_at", "end_at", "location", "department",
        "attending", "admit_source", "disposition", "priority"), "date": "start_at"},
    "condition": {"table": "ehr_conditions", "cols": (
        "clinical_status", "verification", "category", "severity", "onset", "abatement"),
        "date": "onset"},
    "allergy": {"table": "ehr_allergies", "cols": (
        "reaction", "severity", "criticality", "category", "onset"), "date": "onset"},
    "medication": {"table": "ehr_medications", "cols": (
        "kind", "dose", "route", "frequency", "frequency_hours", "start_at", "end_at",
        "prescriber", "indication"), "date": "start_at"},
    "observation": {"table": "ehr_observations", "cols": (
        "category", "value_num", "value_text", "unit", "ref_low", "ref_high", "interpretation",
        "effective", "performer", "panel_id"), "date": "effective"},
    "procedure": {"table": "ehr_procedures", "cols": (
        "performed", "performer", "body_site", "outcome"), "date": "performed"},
    "immunization": {"table": "ehr_immunizations", "cols": (
        "occurrence", "lot", "dose_number", "site", "route", "performer"), "date": "occurrence"},
    "document": {"table": "ehr_documents", "cols": (
        "doc_type", "title", "author", "content_type", "content", "size_bytes", "sha256",
        "effective"), "date": "effective"},
    "service_request": {"table": "ehr_service_requests", "cols": (
        "category", "intent", "priority", "reason", "requester", "performer", "accession",
        "occurrence"), "date": "occurrence"},
    "diagnostic_report": {"table": "ehr_diagnostic_reports", "cols": (
        "category", "effective", "issued", "conclusion", "study_uid", "performer",
        "result_ids"), "date": "effective"},
    "consent": {"table": "ehr_consents", "cols": (
        "scope", "category", "grantee", "purposes", "start_at", "end_at"), "date": "start_at"},
}

_hooks: list = []


def register_hook(fn) -> None:
    """``fn(resource_type, row, action)`` after create/update/delete."""
    if fn not in _hooks:
        _hooks.append(fn)


def _spec(rtype: str) -> dict:
    if rtype not in RESOURCES:
        raise ValueError(f"unknown resource type {rtype!r}")
    return RESOURCES[rtype]


def _decode(rtype: str, r) -> Optional[dict]:
    d = row(r)
    if d is None:
        return None
    d["resource_type"] = rtype
    d["data"] = jload(d.get("data"), {})
    if rtype == "document" and d.get("content") is not None:
        try:
            d["content"] = phi_crypto.decrypt_json(d["content"], patient_id=d["person_id"],
                                                   owner_user_id=None)
        except ValueError:
            log.error("document %s could not be decrypted", d["id"])
            d["content"] = None
            d["content_error"] = "undecryptable"
    for k in ("result_ids", "purposes"):
        if k in d:
            d[k] = jload(d.get(k), [])
    d["deleted"] = bool(d.get("deleted"))
    return d


def _encode(rtype: str, values: dict) -> dict:
    out = dict(values)
    if "data" in out and not isinstance(out["data"], str):
        out["data"] = jdump(out["data"] or {})
    for k in ("result_ids", "purposes"):
        if k in out and not isinstance(out[k], str):
            out[k] = jdump(out[k] or [])
    if rtype == "document" and "content" in out and out["content"] is not None:
        text = out["content"] if isinstance(out["content"], str) else str(out["content"])
        out.setdefault("size_bytes", len(text.encode()))
        out.setdefault("sha256", hashlib.sha256(text.encode()).hexdigest())
        out["content"] = phi_crypto.encrypt_json(text, patient_id=values["person_id"],
                                                 owner_user_id=None)
    return out


def _snapshot(c, rtype: str, rid: str, actor: Optional[str]) -> None:
    spec = _spec(rtype)
    r = c.execute(f"SELECT * FROM {spec['table']} WHERE id=?", (rid,)).fetchone()
    if r:
        c.execute("INSERT INTO ehr_history(id, resource, resource_id, version, snapshot, "
                  "changed_by, changed_at) VALUES (?,?,?,?,?,?,?)",
                  (new_id(), rtype, rid, int(r["version"]), jdump(dict(r)), actor, db.now()))


def create(rtype: str, values: dict[str, Any], *, actor: Optional[str] = None,
           rid: Optional[str] = None) -> dict:
    """Insert a resource. If (source_facility, source_id) already exists, the
    existing row is updated instead (idempotent import)."""
    spec = _spec(rtype)
    if not values.get("person_id"):
        raise ValueError("person_id is required")
    pid = mpi.resolve(values["person_id"])
    if not pid:
        raise ValueError("unknown person")
    allowed = set(_COMMON) | set(spec["cols"])
    vals = {k: v for k, v in values.items() if k in allowed}
    vals["person_id"] = pid
    vals.setdefault("facility_oid", settings.facility_oid())
    vals.setdefault("source_facility", settings.facility_oid())
    vals.setdefault("created_by", actor)
    if vals.get("source_id"):
        with db.connect() as c:
            ex = c.execute(f"SELECT id FROM {spec['table']} WHERE source_facility=? AND source_id=?",
                           (vals["source_facility"], vals["source_id"])).fetchone()
        if ex:
            return update(rtype, ex["id"], {k: v for k, v in vals.items()
                                            if k not in ("source_facility", "source_id")},
                          actor=actor, deleted=False)
    rid = rid or new_id()
    vals.setdefault("source_id", rid)
    enc = _encode(rtype, vals)
    now = db.now()
    cols = list(enc) + ["id", "created_at", "updated_at"]
    with db.connect() as c:
        c.execute(f"INSERT INTO {spec['table']}({', '.join(cols)}) VALUES "
                  f"({', '.join('?' for _ in cols)})", (*enc.values(), rid, now, now))
    out = get(rtype, rid)
    for h in _hooks:
        h(rtype, out, "create")
    return out  # type: ignore[return-value]


def update(rtype: str, rid: str, changes: dict[str, Any], *, actor: Optional[str] = None,
           expected_version: Optional[int] = None, deleted: Optional[bool] = None) -> dict:
    spec = _spec(rtype)
    allowed = (set(_COMMON) | set(spec["cols"])) - {"person_id", "facility_oid"}
    cur = get(rtype, rid, include_deleted=True)
    if not cur:
        raise KeyError(rid)
    if expected_version is not None and int(cur["version"]) != int(expected_version):
        raise ValueError(f"version conflict: current is {cur['version']}")
    vals = {k: v for k, v in changes.items() if k in allowed}
    vals = _encode(rtype, {**vals, "person_id": cur["person_id"]})
    vals.pop("person_id", None)
    if deleted is not None:
        vals["deleted"] = 1 if deleted else 0
    with db.connect() as c:
        _snapshot(c, rtype, rid, actor)
        sets = ", ".join(f"{k}=?" for k in vals)
        c.execute(f"UPDATE {spec['table']} SET {sets + ', ' if sets else ''}version=version+1, "
                  "updated_at=? WHERE id=?", (*vals.values(), db.now(), rid))
    out = get(rtype, rid, include_deleted=True)
    for h in _hooks:
        h(rtype, out, "update")
    return out  # type: ignore[return-value]


def delete(rtype: str, rid: str, *, actor: Optional[str] = None) -> bool:
    if not get(rtype, rid):
        return False
    update(rtype, rid, {}, actor=actor, deleted=True)
    return True


def get(rtype: str, rid: str, *, include_deleted: bool = False) -> Optional[dict]:
    spec = _spec(rtype)
    with db.connect() as c:
        r = c.execute(f"SELECT * FROM {spec['table']} WHERE id=?", (rid,)).fetchone()
    d = _decode(rtype, r)
    if d and d["deleted"] and not include_deleted:
        return None
    return d


def history(rtype: str, rid: str) -> list[dict]:
    with db.connect() as c:
        rows = c.execute("SELECT * FROM ehr_history WHERE resource=? AND resource_id=? "
                         "ORDER BY version", (rtype, rid)).fetchall()
    out = []
    for r in rows:
        snap = jload(r["snapshot"], {})
        out.append(_decode(rtype, snap) if snap else None)
    cur = get(rtype, rid, include_deleted=True)
    return [x for x in out if x] + ([cur] if cur else [])


def list_for(rtype: str, person_id: str, *, filters: Optional[dict[str, Any]] = None,
             limit: int = 500, offset: int = 0, include_deleted: bool = False) -> list[dict]:
    spec = _spec(rtype)
    pid = mpi.resolve(person_id) or person_id
    clauses, params = ["person_id=?"], [pid]
    if not include_deleted:
        clauses.append("deleted=0")
    allowed = set(_COMMON) | set(spec["cols"])
    for k, v in (filters or {}).items():
        if k in allowed and v is not None:
            clauses.append(f"{k}=?")
            params.append(v)
    order = spec.get("date") or "created_at"
    with db.connect() as c:
        rows = c.execute(f"SELECT * FROM {spec['table']} WHERE {' AND '.join(clauses)} "
                         f"ORDER BY {order} DESC, created_at DESC LIMIT ? OFFSET ?",
                         (*params, limit, offset)).fetchall()
    return [_decode(rtype, r) for r in rows]


def search(rtype: str, filters: dict[str, Any], *, limit: int = 100, offset: int = 0) -> list[dict]:
    """Cross-patient search on exact column filters (FHIR server use)."""
    spec = _spec(rtype)
    allowed = set(_COMMON) | set(spec["cols"]) | {"id"}
    clauses, params = ["deleted=0"], []
    for k, v in filters.items():
        if k in allowed and v is not None:
            if isinstance(v, (list, tuple)):
                clauses.append(f"{k} IN ({', '.join('?' for _ in v)})")
                params.extend(v)
            else:
                clauses.append(f"{k}=?")
                params.append(v)
    order = spec.get("date") or "created_at"
    with db.connect() as c:
        rows = c.execute(f"SELECT * FROM {spec['table']} WHERE {' AND '.join(clauses)} "
                         f"ORDER BY {order} DESC LIMIT ? OFFSET ?",
                         (*params, limit, offset)).fetchall()
    return [_decode(rtype, r) for r in rows]


def replace_source_set(rtype: str, person_id: str, source_prefix: str,
                       items: Iterable[dict[str, Any]], *, actor: Optional[str] = None) -> int:
    """Make the rows whose source_id starts with *source_prefix* equal *items*.

    Used by projections (legacy EHR bridge, re-imported packages): items carry
    their own stable ``source_id``; rows no longer present are soft-deleted.
    """
    spec = _spec(rtype)
    keep: set[str] = set()
    n = 0
    for it in items:
        it = {**it, "person_id": person_id}
        created = create(rtype, it, actor=actor)
        keep.add(created["id"])
        n += 1
    with db.connect() as c:
        stale = [r["id"] for r in c.execute(
            f"SELECT id FROM {spec['table']} WHERE person_id=? AND source_facility=? "
            "AND source_id LIKE ? AND deleted=0",
            (person_id, settings.facility_oid(), source_prefix + "%")).fetchall()]
    for rid in stale:
        if rid not in keep:
            delete(rtype, rid, actor=actor)
    return n


# MPI merges repoint every EHR table.
def _on_merge(conn, survivor: str, merged: str) -> None:
    # Document content is encrypted with the person id as associated data, so
    # it must be re-encrypted under the survivor before the id changes.
    for r in conn.execute("SELECT id, content FROM ehr_documents WHERE person_id=? "
                          "AND content IS NOT NULL", (merged,)).fetchall():
        plain = phi_crypto.decrypt_json(r["content"], patient_id=merged, owner_user_id=None)
        conn.execute("UPDATE ehr_documents SET content=? WHERE id=?",
                     (phi_crypto.encrypt_json(plain, patient_id=survivor, owner_user_id=None),
                      r["id"]))
    for spec in RESOURCES.values():
        conn.execute(f"UPDATE {spec['table']} SET person_id=? WHERE person_id=?",
                     (survivor, merged))
    for table in ("ehr_breakglass", "ehr_transfers", "ems_notifications"):
        conn.execute(f"UPDATE {table} SET person_id=? WHERE person_id=?", (survivor, merged))


mpi.register_merge_hook(_on_merge)
