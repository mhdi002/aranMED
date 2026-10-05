"""Inter-hospital patient transfer workflow.

Both hospitals keep their own ``ehr_transfers`` row, linked by
``remote_id``; every state change is pushed to the other side with a signed
peer token (purpose ``TRANSFER``) so the two views stay in step.

    sending (A)                                 receiving (B)
    ───────────                                 ─────────────
    request  ── POST /api/transfers/inbound ──▶  requested (patient pre-registered in B's MPI)
    accepted ◀─ POST …/inbound/{id}/status ───  accept | reject
    package  ── POST …/inbound/{id}/package ─▶  record imported (FHIR transaction:
             ── STOW-RS studies ──────────────▶   Patient + clinical resources + summary);
                                                 imaging stored in B's PACS
    in_transit ─ status ─────────────────────▶  in_transit  (EMS unit, ETA)
    arrived  ◀── status ───────────────────────  arrived
    completed◀── status ───────────────────────  completed

The package is the patient's full local record (``Patient/$everything``)
plus a transfer-summary DocumentReference; imaging moves by STOW-RS (or is
left for on-demand federated retrieval with ``include_imaging=false``).
Records keep their authoring facility, so B can tell what came from A, and a
re-sent package de-duplicates instead of doubling the chart.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

import httpx

import audit
import db
from clinicaldb import facilities, messages, mpi, settings
from clinicaldb import principal as pr
from clinicaldb.util import jdump, new_id
from ehr import chart as chart_mod, store, transfers_view
from interop import fhir_map

log = logging.getLogger("interop.transfers")

STATES = ("requested", "accepted", "rejected", "cancelled", "in_transit", "arrived", "completed")
_NEXT = {"requested": {"accepted", "rejected", "cancelled"},
         "accepted": {"in_transit", "cancelled", "arrived"},
         "in_transit": {"arrived"}, "arrived": {"completed"}}


class TransferError(ValueError):
    pass


def get(tid: str) -> Optional[dict]:
    with db.connect() as c:
        r = c.execute("SELECT * FROM ehr_transfers WHERE id=?", (tid,)).fetchone()
    return transfers_view.decode(r) if r else None


def by_remote(transfer_ref: str, peer_oid: str) -> Optional[dict]:
    """The local row a peer refers to: peers address *our* id (which they
    store as their remote_id); the initial request carries *their* id."""
    with db.connect() as c:
        r = c.execute("SELECT * FROM ehr_transfers WHERE (id=? OR remote_id=?) AND "
                      "(from_facility=? OR to_facility=?)",
                      (transfer_ref, transfer_ref, peer_oid, peer_oid)).fetchone()
    return transfers_view.decode(r) if r else None


def list_all(*, direction: Optional[str] = None, status: Optional[str] = None,
             limit: int = 200) -> list[dict]:
    clauses, params = [], []
    if direction:
        clauses.append("direction=?")
        params.append(direction)
    if status:
        clauses.append("status=?")
        params.append(status)
    sql = "SELECT * FROM ehr_transfers" + (" WHERE " + " AND ".join(clauses) if clauses else "")
    with db.connect() as c:
        rows = c.execute(sql + " ORDER BY updated_at DESC LIMIT ?", (*params, limit)).fetchall()
    out = []
    for r in rows:
        t = transfers_view.decode(r)
        p = mpi.get(t["person_id"])
        t["patient"] = {"name": p["name"], "demographics": p["demographics"]} if p else None
        for k in ("from_facility", "to_facility"):
            f = facilities.get_by_oid(t[k])
            t[f"{k}_name"] = f["name"] if f else t[k]
        out.append(t)
    return out


def _update(tid: str, *, status: Optional[str] = None, by: Optional[str] = None,
            note: Optional[str] = None, **fields: Any) -> dict:
    t = get(tid)
    hist = t["history"]
    if status:
        hist.append({"status": status, "at": db.now(), "by": by, "note": note})
        fields["status"] = status
    fields["history"] = jdump(hist)
    if "package_manifest" in fields and not isinstance(fields["package_manifest"], str):
        fields["package_manifest"] = jdump(fields["package_manifest"])
    with db.connect() as c:
        c.execute(f"UPDATE ehr_transfers SET {', '.join(f'{k}=?' for k in fields)}, updated_at=? "
                  "WHERE id=?", (*fields.values(), db.now(), tid))
    return get(tid)


def _insert(**vals: Any) -> dict:
    tid = vals.pop("id", None) or new_id()
    now = db.now()
    vals.setdefault("history", jdump([{"status": vals["status"], "at": now, "by": vals.get("requested_by")}]))
    cols = list(vals) + ["id", "created_at", "updated_at"]
    with db.connect() as c:
        c.execute(f"INSERT INTO ehr_transfers({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                  (*vals.values(), tid, now, now))
    return get(tid)


def _peer(oid: str) -> dict:
    f = facilities.get_by_oid(oid)
    if not f or f.get("is_local") or not f.get("active"):
        raise TransferError(f"{oid} is not an active peer facility")
    if not f.get("base_url"):
        raise TransferError(f"peer {f['name']} has no base_url configured")
    return f


def _call(f: dict, method: str, path: str, *, practitioner: str = "system",
          json_body: Any = None, retries: int = 3) -> dict:
    token = pr.token_for_peer(f, practitioner=practitioner, purpose="TRANSFER")
    url = f["base_url"].rstrip("/") + path
    delay = settings.env_float("TRANSFER_RETRY_BASE_SEC", 0.5)
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            r = httpx.request(method, url, json=json_body, timeout=settings.env_float("TRANSFER_HTTP_TIMEOUT_SEC", 120),
                              headers={"Authorization": f"Bearer {token}"})
            if r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            if r.status_code >= 400:
                raise TransferError(f"{f['name']} refused: HTTP {r.status_code} {r.text[:200]}")
            messages.log(direction="out", protocol="transfer", message_type=f"{method} {path.split('/')[-1]}",
                         peer=f["oid"], status="ok")
            return r.json() if r.content else {}
        except TransferError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(delay * (2 ** attempt))
    messages.log(direction="out", protocol="transfer", message_type=path, peer=f["oid"],
                 status="error", error=str(last))
    raise TransferError(f"could not reach {f['name']}: {last}")


def _dangling(reference: str, local: dict) -> bool:
    """A reference to one of this hospital's own records that is not in the
    package (e.g. a deleted encounter) would dangle at the receiver: drop it."""
    rt, _, rid = reference.partition("/")
    return bool(rid) and rt in ("Encounter", "Observation", "DocumentReference") and reference not in local


# ---------------------------------------------------------------- sending side
def request(person_id: str, to_oid: str, *, user: dict, urgency: str = "routine",
            reason: str = "", clinical_summary: str = "", transport_mode: str = "",
            ems_unit: str = "", include_imaging: bool = True) -> dict:
    person = mpi.get(person_id)
    if not person:
        raise TransferError("unknown person")
    f = _peer(to_oid)
    t = _insert(person_id=person["id"], direction="outgoing", from_facility=settings.facility_oid(),
                to_facility=to_oid, status="requested", urgency=urgency, reason=reason,
                clinical_summary=clinical_summary, transport_mode=transport_mode, ems_unit=ems_unit,
                requested_by=user.get("username"), package_status="pending",
                package_manifest=jdump({"include_imaging": include_imaging}))
    try:
        resp = _call(f, "POST", "/api/transfers/inbound", practitioner=user.get("username") or "system",
                     json_body={"transfer_id": t["id"], "patient": fhir_map.patient(person),
                                "urgency": urgency, "reason": reason,
                                "clinical_summary": clinical_summary,
                                "transport_mode": transport_mode, "ems_unit": ems_unit,
                                "include_imaging": include_imaging})
    except TransferError as e:
        _update(t["id"], status="cancelled", by="system", note=f"delivery failed: {e}")
        raise
    audit.record("transfer.request", actor=user, resource=f"person:{person['id']}",
                 detail={"to": to_oid, "transfer": t["id"]})
    return _update(t["id"], remote_id=resp.get("id"))


def build_package(t: dict) -> dict:
    """FHIR transaction Bundle with the whole local record + a summary note.

    Every entry gets a ``urn:uuid`` fullUrl and references between records in
    the package (encounter links, report results, document context) point at
    those, so the receiver resolves them to its own new ids instead of keeping
    dangling ids from this hospital. Medications carry their dose history.
    """
    pid = t["person_id"]
    person = mpi.get(pid)
    entries = [{"fullUrl": f"urn:uuid:patient-{pid}", "resource": fhir_map.patient(person),
                "request": {"method": "POST", "url": "Patient"}}]
    ref = f"urn:uuid:patient-{pid}"
    rows: list[tuple[str, dict]] = []
    for rtype in store.RESOURCES:
        if rtype == "consent":
            continue  # consents are local legal records; the receiver records its own
        for row in store.list_for(rtype, pid, limit=10000):
            rows.append((rtype, row))
    local = {f"{fhir_map.fhir_type(rt, row)}/{row['id']}": f"urn:uuid:{rt}-{row['id']}" for rt, row in rows}

    def relink(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: (local.get(v, None) if k == "reference" and isinstance(v, str) and v in local
                        else relink(v)) for k, v in obj.items()
                    if not (k == "reference" and isinstance(v, str) and _dangling(v, local))}
        if isinstance(obj, list):
            return [x for x in (relink(i) for i in obj) if x != {}]
        return obj

    n = 0
    for rtype, row in rows:
        if rtype == "medication":
            row = {**row, "_dose_history": store.prior_versions(rtype, row["id"])}
        res = fhir_map.to_fhir(rtype, row)
        for key in ("subject", "patient"):
            if key in res:
                res[key] = {"reference": ref}
        res = relink(res)
        for key in ("encounter", "context"):
            if res.get(key) in ({}, None):
                res.pop(key, None)
        entries.append({"fullUrl": f"urn:uuid:{rtype}-{row['id']}", "resource": res,
                        "request": {"method": "POST", "url": res["resourceType"]}})
        n += 1
    c = chart_mod.build(pid)
    summary = (f"TRANSFER SUMMARY\nFrom: {settings.facility_name()} ({settings.facility_oid()})\n"
               f"Reason: {t.get('reason') or '-'}\nUrgency: {t.get('urgency') or '-'}\n"
               f"Clinical summary: {t.get('clinical_summary') or '-'}\n\n{chart_mod.as_text(c)}")
    note = fhir_map.to_fhir("document", {
        "id": f"transfer-{t['id']}", "person_id": pid, "facility_oid": settings.facility_oid(),
        "source_facility": settings.facility_oid(), "source_id": f"transfer-summary:{t['id']}",
        "doc_type": "transfer-summary", "title": f"Transfer summary to {t['to_facility']}",
        "content_type": "text/plain", "content": summary, "status": "current",
        "author": t.get("requested_by"), "effective": time.strftime("%Y-%m-%d"),
        "created_at": db.now(), "updated_at": db.now(), "version": 1})
    note["subject"] = {"reference": ref}
    entries.append({"resource": note, "request": {"method": "POST", "url": "DocumentReference"}})
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries,
            "_counts": {"resources": n + 1}}


def send_package(tid: str, *, practitioner: str = "system") -> dict:
    t = get(tid)
    f = _peer(t["to_facility"])
    bundle = build_package(t)
    counts = bundle.pop("_counts")
    resp = _call(f, "POST", f"/api/transfers/inbound/{t['remote_id']}/package",
                 practitioner=practitioner, json_body=bundle)
    manifest = {**t["package_manifest"], "resources": counts["resources"],
                "imported": resp.get("imported"), "studies": []}
    if manifest.get("include_imaging", True):
        from pacs import index, jobs
        for s in index.query_studies({"x-person-id": t["person_id"]}, limit=500):
            job = jobs.submit("send", target=f"facility:{f['oid']}", study_uid=s["StudyInstanceUID"],
                              params={"practitioner": practitioner, "purpose": "TRANSFER"},
                              created_by=practitioner, wait=True)
            manifest["studies"].append({"study_uid": s["StudyInstanceUID"], "status": job["status"],
                                        "result": job.get("result"), "error": job.get("last_error")})
    ok = all(s["status"] == "done" for s in manifest["studies"])
    return _update(tid, package_status="delivered" if ok else "partial", package_manifest=manifest)


def change_status(tid: str, status: str, *, user: dict, note: str = "", eta: Optional[str] = None,
                  rejection_reason: Optional[str] = None, notify: bool = True) -> dict:
    t = get(tid)
    if not t:
        raise TransferError("unknown transfer")
    if status not in _NEXT.get(t["status"], set()):
        raise TransferError(f"cannot go from {t['status']} to {status}")
    # who may do what
    if t["direction"] == "incoming" and status in ("in_transit",):
        raise TransferError("the sending hospital marks departure")
    if t["direction"] == "outgoing" and status in ("accepted", "rejected", "arrived", "completed"):
        raise TransferError("the receiving hospital records this step")
    fields: dict[str, Any] = {}
    if eta:
        fields["eta"] = eta
    if status == "accepted":
        fields["accepted_by"] = user.get("username")
    if status == "rejected":
        fields["rejection_reason"] = rejection_reason or note
    t = _update(tid, status=status, by=user.get("username"), note=note, **fields)
    audit.record(f"transfer.{status}", actor=user, resource=f"person:{t['person_id']}",
                 detail={"transfer": tid})
    if notify and t.get("remote_id"):
        other = t["from_facility"] if t["direction"] == "incoming" else t["to_facility"]
        try:
            _call(_peer(other), "POST", f"/api/transfers/inbound/{t['remote_id']}/status",
                  practitioner=user.get("username") or "system",
                  json_body={"status": status, "by": user.get("username"), "note": note, "eta": eta,
                             "rejection_reason": fields.get("rejection_reason")})
        except TransferError as e:
            _update(tid, note=f"peer not notified: {e}")
    return t


# ---------------------------------------------------------------- receiving side
def receive_request(body: dict, peer: dict) -> dict:
    demo, idents = fhir_map.patient_in(body["patient"])
    reg = mpi.register_person(demo, idents, source_facility=peer["facility_oid"])
    mpi.ensure_local_mrn(reg["person_id"])
    existing = by_remote(body["transfer_id"], peer["facility_oid"])
    if existing:
        return existing
    t = _insert(person_id=reg["person_id"], direction="incoming", from_facility=peer["facility_oid"],
                to_facility=settings.facility_oid(), remote_id=body["transfer_id"], status="requested",
                urgency=body.get("urgency"), reason=body.get("reason"),
                clinical_summary=body.get("clinical_summary"), transport_mode=body.get("transport_mode"),
                ems_unit=body.get("ems_unit"), requested_by=peer.get("practitioner"),
                package_status="awaiting", package_manifest=jdump({"include_imaging": body.get("include_imaging", True)}))
    return {**t, "mpi_outcome": reg["outcome"]}


def receive_status(remote_id: str, body: dict, peer: dict) -> dict:
    t = by_remote(remote_id, peer["facility_oid"])
    if not t:
        raise TransferError("unknown transfer")
    status = body.get("status")
    if status not in STATES:
        raise TransferError("bad status")
    if status != t["status"] and status not in _NEXT.get(t["status"], set()):
        raise TransferError(f"cannot go from {t['status']} to {status}")
    if status == t["status"]:
        return t  # duplicate notification (e.g. a retry) — nothing to do
    fields: dict[str, Any] = {}
    if body.get("eta"):
        fields["eta"] = body["eta"]
    if status == "accepted":
        fields["accepted_by"] = body.get("by")
    if status == "rejected":
        fields["rejection_reason"] = body.get("rejection_reason")
    t = _update(t["id"], status=status, by=f"{peer['facility_oid']}:{body.get('by') or ''}",
                note=body.get("note"), **fields)
    if t["direction"] == "outgoing" and status == "accepted":
        threading.Thread(target=_send_package_safely, args=(t["id"], body.get("by") or "system"),
                         daemon=True, name="transfer-package").start()
    return t


def _send_package_safely(tid: str, practitioner: str) -> None:
    try:
        send_package(tid, practitioner=practitioner)
    except Exception as e:  # noqa: BLE001
        log.exception("transfer package for %s failed", tid)
        _update(tid, package_status="failed", note=f"package failed: {e}")


def receive_package(remote_id: str, bundle: dict, peer: dict, request) -> dict:
    t = by_remote(remote_id, peer["facility_oid"])
    if not t:
        raise TransferError("unknown transfer")
    if t["status"] not in ("accepted", "in_transit", "arrived"):
        raise TransferError(f"package not expected in state {t['status']}")
    from interop import fhir_server
    # The import runs with the receiving hospital's authority, attributing
    # every record to the sending facility (meta.source keeps the author).
    system = {"id": None, "username": f"transfer:{peer['facility_oid']}", "role": "admin",
              "kind": "user", "facility_oid": settings.facility_oid()}
    for e in bundle.get("entry") or []:
        res = e.get("resource") or {}
        if res.get("resourceType") == "Patient":
            continue
        meta = res.setdefault("meta", {})
        if not (meta.get("source") or "").startswith("urn:oid:"):
            meta["source"] = f"urn:oid:{peer['facility_oid']}#{res.get('id')}"
    # The bundle's Patient IS the person this transfer is for: add its
    # identifiers there and point every record at that person, rather than
    # letting demographic matching decide (it could create a duplicate).
    kept = []
    for e in bundle.get("entry") or []:
        res = e.get("resource") or {}
        if res.get("resourceType") == "Patient":
            demo, idents = fhir_map.patient_in(res)
            for i in idents:
                mpi.add_identifier(t["person_id"], i["system"], i["value"], type_=i["type"],
                                   facility_oid=i.get("facility_oid"))
            continue
        kept.append(e)
    me = {"reference": f"Patient/{t['person_id']}"}
    for e in kept:
        res = e["resource"]
        for key in ("subject", "patient"):
            if key in res:
                res[key] = me
    bundle = {**bundle, "entry": kept}
    resp = fhir_server.process_bundle(bundle, system, request)
    counts: dict[str, int] = {}
    for e in resp["entry"]:
        r = e.get("resource") or {}
        if r:
            counts[r["resourceType"]] = counts.get(r["resourceType"], 0) + 1
    manifest = {**t["package_manifest"], "imported": counts, "received_at": db.now()}
    _update(t["id"], package_status="received", package_manifest=manifest,
            note=f"package received: {sum(counts.values())} resources")
    audit.record("transfer.package.received", actor_name=f"peer:{peer['facility_oid']}",
                 resource=f"person:{t['person_id']}", detail={"counts": counts})
    return {"transfer_id": t["id"], "imported": counts}
