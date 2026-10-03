"""FHIR R4 RESTful server over the relational EHR, MPI and PACS.

Mounted at ``FHIR_PREFIX`` (default ``/api/fhir/r4``). Serves this
hospital's clinicians (session token) and peer facilities (signed peer
token; consent policy applied per patient — see :mod:`ehr.access`).

Interactions: ``metadata``, ``read``, ``vread``, ``history``, ``search``
(type-level, with ``_count``/``_offset`` paging), ``create``, ``update``
(``If-Match`` version check), ``delete``, and ``transaction``/``batch``
Bundles with ``urn:uuid`` reference resolution.

Operations:
* ``Patient/$everything``   — the whole record as a searchset Bundle
* ``Patient/$summary``      — a document Bundle (Composition + sections)
* ``Patient/$match``        — IHE PDQm: scored demographic matching
* ``Patient/$ihe-pix``      — IHE PIXm: cross-reference identifiers
Resources: Patient, Encounter, Condition, AllergyIntolerance,
MedicationStatement, MedicationRequest, Observation, Procedure,
Immunization, DocumentReference (IHE MHD), ServiceRequest,
DiagnosticReport, Consent, ImagingStudy, Organization, Endpoint.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Request, Response

import audit
from clinicaldb import facilities, mpi, settings
from clinicaldb import principal as pr
from ehr import access, chart as chart_mod, store
from interop import fhir_map

log = logging.getLogger("interop.fhir")

FHIR_JSON = "application/fhir+json"
CLINICAL = ["Encounter", "Condition", "AllergyIntolerance", "MedicationStatement",
            "MedicationRequest", "Observation", "Procedure", "Immunization",
            "DocumentReference", "ServiceRequest", "DiagnosticReport", "Consent"]
_SEARCH_PARAMS = {
    "Patient": ["_id", "identifier", "name", "family", "given", "birthdate", "gender"],
    "_clinical": ["_id", "patient", "subject", "category", "code", "status", "encounter", "date"],
    "ImagingStudy": ["_id", "patient", "subject", "identifier", "modality", "started"],
}


class FhirError(Exception):
    def __init__(self, status: int, code: str, diagnostics: str) -> None:
        super().__init__(diagnostics)
        self.status, self.code, self.diagnostics = status, code, diagnostics


def prefix() -> str:
    return "/" + settings.env("FHIR_PREFIX", "/api/fhir/r4").strip("/")


def _resp(body: Any, status: int = 200, headers: Optional[dict] = None) -> Response:
    return Response(json.dumps(body, ensure_ascii=False), status_code=status,
                    media_type=FHIR_JSON, headers=headers)


def outcome(status: int, code: str, diag: str) -> Response:
    return _resp({"resourceType": "OperationOutcome",
                  "issue": [{"severity": "error" if status >= 400 else "information",
                             "code": code, "diagnostics": diag}]}, status)


def _base(request: Request) -> str:
    return settings.public_base_url() + prefix() if settings.public_base_url() \
        else str(request.base_url).rstrip("/") + prefix()


def _principal(request: Request, permission: str) -> dict:
    from fastapi import HTTPException
    try:
        p = pr.resolve_principal(request)
    except HTTPException as e:
        raise FhirError(e.status_code, "login", str(e.detail)) from e
    if not pr.allows(p, permission):
        audit.record("rbac.deny", **pr.actor(p), resource=permission, outcome="deny",
                     detail={"path": request.url.path})
        raise FhirError(403, "forbidden", f"not permitted to {permission}")
    return p


def _check_person(p: dict, person_id: str, request: Request, action: str) -> str:
    person = mpi.get(person_id)
    if not person:
        raise FhirError(404, "not-found", f"Patient/{person_id} not found")
    ok, why = access.decide(person["id"], p)
    audit.record(action, **pr.actor(p), resource=f"person:{person['id']}",
                 outcome="allow" if ok else "deny",
                 client_ip=request.client.host if request.client else None,
                 detail={"why": why, "kind": p.get("kind"), "purpose": p.get("purpose"),
                         "via": "fhir"})
    if not ok:
        raise FhirError(403, "forbidden", why)
    return person["id"]


def _bundle(kind: str, entries: list[dict], base: str, total: Optional[int] = None,
            links: Optional[list] = None) -> dict:
    b = {"resourceType": "Bundle", "id": uuid.uuid4().hex, "type": kind,
         "meta": {"lastUpdated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
         "entry": [{"fullUrl": f"{base}/{e['resourceType']}/{e['id']}", "resource": e,
                    **({"search": {"mode": "match"}} if kind == "searchset" else {})}
                   for e in entries]}
    if total is not None:
        b["total"] = total
    if links:
        b["link"] = links
    return b


def _rtype_for(resource_type: str) -> str:
    rt = fhir_map.FHIR_TO_TYPE.get(resource_type)
    if not rt:
        raise FhirError(404, "not-supported", f"resource type {resource_type} not supported")
    return rt


def _row_matches_type(resource_type: str, row: dict) -> bool:
    if resource_type == "MedicationStatement":
        return row.get("kind") != "request"
    if resource_type == "MedicationRequest":
        return row.get("kind") == "request"
    return True


# ---------------------------------------------------------------- capability
def capability(request: Request) -> dict:
    def res(t: str, params: list[str], write: bool = True) -> dict:
        inter = [{"code": c} for c in ("read", "vread", "history-instance", "search-type")]
        if write:
            inter += [{"code": c} for c in ("create", "update", "delete")]
        out = {"type": t, "interaction": inter, "versioning": "versioned",
               "searchParam": [{"name": n, "type": "token" if n in ("_id", "identifier", "category", "code", "status", "gender", "modality")
                                else "date" if n in ("date", "birthdate", "started") else "reference" if n in ("patient", "subject", "encounter")
                                else "string"} for n in params]}
        if t == "Patient":
            out["operation"] = [{"name": n, "definition": d} for n, d in (
                ("everything", "http://hl7.org/fhir/OperationDefinition/Patient-everything"),
                ("summary", "http://hl7.org/fhir/uv/ips/OperationDefinition/summary"),
                ("match", "http://hl7.org/fhir/OperationDefinition/Patient-match"),
                ("ihe-pix", "https://profiles.ihe.net/ITI/PIXm/OperationDefinition/IHE.PIXm.pix"))]
        return out
    resources = [res("Patient", _SEARCH_PARAMS["Patient"])]
    resources += [res(t, _SEARCH_PARAMS["_clinical"]) for t in CLINICAL]
    resources += [res("ImagingStudy", _SEARCH_PARAMS["ImagingStudy"], write=False),
                  res("Organization", ["_id"], write=False), res("Endpoint", ["_id"], write=False)]
    loc = facilities.local()
    return {"resourceType": "CapabilityStatement", "status": "active",
            "date": time.strftime("%Y-%m-%d"), "kind": "instance", "fhirVersion": "4.0.1",
            "format": ["json"], "publisher": loc["name"],
            "software": {"name": "AranMed FHIR server", "version": "1.0"},
            "implementation": {"description": f"{loc['name']} ({loc['oid']})", "url": _base(request)},
            "rest": [{"mode": "server", "resource": resources,
                      "interaction": [{"code": "transaction"}, {"code": "batch"}],
                      "security": {"description": "Bearer session token (users) or signed peer "
                                                  "token (facilities, with purpose of use)."}}]}


# ---------------------------------------------------------------- read
def read(resource_type: str, rid: str, p: dict, request: Request) -> dict:
    if resource_type == "Patient":
        pid = _check_person(p, rid, request, "fhir.read")
        return fhir_map.patient(mpi.get(pid, follow=False) if pid == rid else mpi.get(pid))
    if resource_type == "Organization":
        f = facilities.get_by_oid(rid)
        if not f:
            raise FhirError(404, "not-found", "Organization not found")
        return fhir_map.organization(f)
    if resource_type == "Endpoint":
        oid = rid.replace("dicomweb-", "", 1)
        f = facilities.get_by_oid(oid)
        if not f:
            raise FhirError(404, "not-found", "Endpoint not found")
        return fhir_map.endpoint(f)
    if resource_type == "ImagingStudy":
        from pacs import index
        s = index.get_study(rid)
        if not s:
            raise FhirError(404, "not-found", "ImagingStudy not found")
        if s["_ext"].get("person_id"):
            _check_person(p, s["_ext"]["person_id"], request, "fhir.read")
        return fhir_map.imaging_study(s, index.query_series({"StudyInstanceUID": rid}))
    rtype = _rtype_for(resource_type)
    row = store.get(rtype, rid)
    if not row or not _row_matches_type(resource_type, row):
        raise FhirError(404, "not-found", f"{resource_type}/{rid} not found")
    _check_person(p, row["person_id"], request, "fhir.read")
    return fhir_map.to_fhir(rtype, row)


# ---------------------------------------------------------------- search
def _date_filter(values: list[str], col: str, rows: list[dict]) -> list[dict]:
    for v in values:
        op, d = ("eq", v)
        for pfx in ("ge", "le", "gt", "lt", "eq"):
            if v.startswith(pfx):
                op, d = pfx, v[2:]
                break
        def ok(r: dict) -> bool:
            x = str(r.get(col) or "")[:len(d)]
            if not x:
                return False
            return {"eq": x == d, "ge": x >= d, "le": x <= d, "gt": x > d, "lt": x < d}[op]
        rows = [r for r in rows if ok(r)]
    return rows


def search(resource_type: str, params: dict[str, list[str]], p: dict, request: Request) -> dict:
    base = _base(request)
    count = max(1, min(int((params.get("_count") or ["50"])[0]), 500))
    offset = max(0, int((params.get("_offset") or ["0"])[0]))
    entries: list[dict] = []
    if resource_type == "Patient":
        ident = (params.get("identifier") or [None])[0]
        hits: list[dict] = []
        if (params.get("_id") or [None])[0]:
            person = mpi.get(params["_id"][0])
            hits = [{"person_id": person["id"]}] if person else []
        elif ident or params.get("name") or params.get("family") or params.get("given") or params.get("birthdate"):
            name = " ".join(x for k in ("name", "family", "given") for x in params.get(k, []))
            hits = mpi.search(name=name or None, identifier=ident,
                              birth_date=(params.get("birthdate") or [None])[0],
                              sex=(params.get("gender") or [None])[0], limit=500)
        else:
            raise FhirError(400, "too-costly", "Patient search needs identifier, name or birthdate")
        withheld = []
        for h in hits:
            ok, why = access.decide(h["person_id"], p)
            if ok:
                entries.append(fhir_map.patient(mpi.get(h["person_id"])))
            else:
                withheld.append(why)
                audit.record("fhir.search", **pr.actor(p), resource=f"person:{h['person_id']}",
                             outcome="deny", detail={"why": why, "kind": p.get("kind"),
                                                     "purpose": p.get("purpose")})
        audit.record("fhir.search", **pr.actor(p), resource="Patient",
                     detail={"results": len(entries), "kind": p.get("kind"), "purpose": p.get("purpose")})
    elif resource_type == "ImagingStudy":
        from pacs import index
        pref = (params.get("patient") or params.get("subject") or [None])[0]
        f: dict[str, str] = {}
        if pref:
            pid = _check_person(p, pref.split("/")[-1], request, "fhir.search")
            f["x-person-id"] = pid
        elif p.get("kind") == "peer":
            raise FhirError(400, "too-costly", "peers must search ImagingStudy by patient")
        if params.get("_id"):
            f["StudyInstanceUID"] = params["_id"][0]
        if params.get("identifier"):
            f["StudyInstanceUID"] = params["identifier"][0].split("urn:oid:")[-1]
        if params.get("modality"):
            f["ModalitiesInStudy"] = params["modality"][0].split("|")[-1]
        for s in index.query_studies(f, limit=500):
            entries.append(fhir_map.imaging_study(s, index.query_series(
                {"StudyInstanceUID": s["StudyInstanceUID"]})))
    elif resource_type in ("Organization", "Endpoint"):
        for fac in facilities.list_all(include_inactive=False):
            if resource_type == "Organization":
                entries.append(fhir_map.organization(fac))
            elif fac.get("dicomweb_base") or fac.get("is_local"):
                entries.append(fhir_map.endpoint(fac))
    else:
        rtype = _rtype_for(resource_type)
        pref = (params.get("patient") or params.get("subject") or [None])[0]
        filters: dict[str, Any] = {}
        if params.get("_id"):
            filters["id"] = params["_id"][0].split(",")
        if pref:
            filters["person_id"] = _check_person(p, pref.split("/")[-1], request, "fhir.search")
        elif p.get("kind") == "peer" and not params.get("_id"):
            raise FhirError(400, "too-costly", "peers must search by patient")
        if params.get("status"):
            filters["status"] = params["status"][0]
        if params.get("encounter"):
            filters["encounter_id"] = params["encounter"][0].split("/")[-1]
        rows = store.search(rtype, filters, limit=5000)
        rows = [r for r in rows if _row_matches_type(resource_type, r)]
        if params.get("category"):
            cat = params["category"][0].split("|")[-1]
            rows = [r for r in rows if (r.get("category") or r.get("doc_type") or "") == cat]
        if params.get("code"):
            tok = params["code"][0]
            sys_, code = tok.split("|", 1) if "|" in tok else (None, tok)
            rows = [r for r in rows if r.get("code") == code and (not sys_ or r.get("code_system") == sys_)]
        if params.get("date"):
            col = store.RESOURCES[rtype].get("date") or "created_at"
            rows = _date_filter(params["date"], col, rows)
        if not pref:
            rows = [r for r in rows if access.decide(r["person_id"], p)[0]]
        entries = [fhir_map.to_fhir(rtype, r) for r in rows]
    total = len(entries)
    page = entries[offset:offset + count]
    outcome_entry = None
    if resource_type == "Patient" and locals().get("withheld"):
        # Tell the requester that matches exist but were withheld (consent),
        # so a clinician is not misled into thinking there is no history.
        outcome_entry = {"resource": {"resourceType": "OperationOutcome", "id": uuid.uuid4().hex,
                                      "issue": [{"severity": "warning", "code": "suppressed",
                                                 "diagnostics": f"{len(withheld)} match(es) withheld: {withheld[0]}"}]},
                         "search": {"mode": "outcome"}}
    q = "&".join(f"{k}={v}" for k, vs in params.items() if k not in ("_offset",) for v in vs)
    links = [{"relation": "self", "url": f"{base}/{resource_type}?{q}&_offset={offset}"}]
    if offset + count < total:
        links.append({"relation": "next", "url": f"{base}/{resource_type}?{q}&_offset={offset + count}"})
    b = _bundle("searchset", page, base, total=total, links=links)
    if outcome_entry:
        b["entry"].append(outcome_entry)
    return b


# ---------------------------------------------------------------- write
def create(resource_type: str, res: dict, p: dict, request: Request,
           rid: Optional[str] = None) -> tuple[dict, int]:
    if res.get("resourceType") != resource_type:
        raise FhirError(400, "invalid", "resourceType does not match the endpoint")
    if resource_type == "Patient":
        demo, idents = fhir_map.patient_in(res)
        src_fac, _ = fhir_map._parse_source(res)  # noqa: SLF001
        reg = mpi.register_person(demo, idents, source_facility=src_fac)
        mpi.ensure_local_mrn(reg["person_id"])
        audit.record("fhir.create", **pr.actor(p), resource=f"person:{reg['person_id']}",
                     detail={"outcome": reg["outcome"], "kind": p.get("kind")})
        return fhir_map.patient(mpi.get(reg["person_id"])), 201 if reg["outcome"].startswith("created") else 200
    rtype, values = fhir_map.from_fhir(res)
    if not values.get("person_id") or not mpi.get(values["person_id"]):
        raise FhirError(422, "invalid", "subject/patient must reference a known Patient")
    _check_person(p, values["person_id"], request, "fhir.write")
    if p.get("kind") == "peer":
        values.setdefault("source_facility", p["facility_oid"])
    row = store.create(rtype, values, actor=p.get("username"), rid=rid)
    audit.record("fhir.create", **pr.actor(p), resource=f"person:{row['person_id']}",
                 detail={"type": resource_type, "id": row["id"], "kind": p.get("kind")})
    return fhir_map.to_fhir(rtype, row), 201


def update(resource_type: str, rid: str, res: dict, p: dict, request: Request,
           if_match: Optional[str]) -> tuple[dict, int]:
    if resource_type == "Patient":
        person = mpi.get(rid)
        if not person:
            raise FhirError(404, "not-found", "Patient not found")
        _check_person(p, rid, request, "fhir.write")
        demo, idents = fhir_map.patient_in(res)
        mpi.update_demographics(person["id"], demo)
        for i in idents:
            mpi.add_identifier(person["id"], i["system"], i["value"], type_=i["type"],
                               facility_oid=i.get("facility_oid"))
        return fhir_map.patient(mpi.get(person["id"])), 200
    rtype = _rtype_for(resource_type)
    cur = store.get(rtype, rid)
    if not cur:
        r, _ = create(resource_type, {**res, "id": rid}, p, request, rid=rid)
        return r, 201
    _check_person(p, cur["person_id"], request, "fhir.write")
    _, values = fhir_map.from_fhir(res)
    values.pop("person_id", None)
    values.pop("source_facility", None)
    values.pop("source_id", None)
    expected = None
    if if_match:
        expected = int(if_match.replace('W/', '').strip('"'))
    try:
        row = store.update(rtype, rid, values, actor=p.get("username"), expected_version=expected)
    except ValueError as e:
        raise FhirError(412, "conflict", str(e)) from e
    return fhir_map.to_fhir(rtype, row), 200


def delete(resource_type: str, rid: str, p: dict, request: Request) -> None:
    rtype = _rtype_for(resource_type)
    cur = store.get(rtype, rid)
    if not cur:
        return
    _check_person(p, cur["person_id"], request, "fhir.delete")
    store.delete(rtype, rid, actor=p.get("username"))


# ---------------------------------------------------------------- bundles
def _resolve_refs(obj: Any, mapping: dict[str, str]) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "reference" and isinstance(v, str) and v in mapping:
                out[k] = mapping[v]
            else:
                out[k] = _resolve_refs(v, mapping)
        return out
    if isinstance(obj, list):
        return [_resolve_refs(x, mapping) for x in obj]
    return obj


def process_bundle(bundle: dict, p: dict, request: Request) -> dict:
    kind = bundle.get("type")
    if kind not in ("transaction", "batch"):
        raise FhirError(400, "invalid", "Bundle.type must be transaction or batch")
    entries = bundle.get("entry") or []
    # Patients first so clinical resources can reference them by urn:uuid.
    order = sorted(range(len(entries)),
                   key=lambda i: 0 if (entries[i].get("resource") or {}).get("resourceType") == "Patient" else 1)
    mapping: dict[str, str] = {}
    created: list[tuple[str, str]] = []
    results: dict[int, dict] = {}
    try:
        for i in order:
            e = entries[i]
            req = e.get("request") or {}
            method = (req.get("method") or "POST").upper()
            url = req.get("url") or ""
            res = _resolve_refs(e.get("resource") or {}, mapping)
            try:
                if method == "POST":
                    out, status = create(res.get("resourceType") or url.split("/")[0], res, p, request)
                    if e.get("fullUrl"):
                        mapping[e["fullUrl"]] = f"{out['resourceType']}/{out['id']}"
                    if out["resourceType"] != "Patient":
                        created.append((fhir_map.FHIR_TO_TYPE[out["resourceType"]], out["id"]))
                elif method == "PUT":
                    rt, rid = url.split("/")[:2]
                    out, status = update(rt, rid, res, p, request, None)
                    if e.get("fullUrl"):
                        mapping[e["fullUrl"]] = f"{rt}/{rid}"
                elif method == "DELETE":
                    rt, rid = url.split("/")[:2]
                    delete(rt, rid, p, request)
                    out, status = None, 204
                elif method == "GET":
                    rt, _, rid = url.partition("/")
                    out, status = read(rt, rid, p, request), 200
                else:
                    raise FhirError(400, "not-supported", f"method {method}")
                results[i] = {"response": {"status": str(status),
                                           **({"location": f"{out['resourceType']}/{out['id']}/_history/{out.get('meta', {}).get('versionId', '1')}"} if out else {})},
                              **({"resource": out} if out else {})}
            except FhirError as fe:
                if kind == "transaction":
                    raise
                results[i] = {"response": {"status": str(fe.status), "outcome": {
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "error", "code": fe.code, "diagnostics": fe.diagnostics}]}}}
    except FhirError:
        # all-or-nothing: compensate everything this transaction created
        for rtype, rid in reversed(created):
            try:
                store.delete(rtype, rid, actor="transaction-rollback")
            except Exception:  # noqa: BLE001
                log.exception("rollback of %s/%s failed", rtype, rid)
        raise
    return {"resourceType": "Bundle", "id": uuid.uuid4().hex,
            "type": f"{kind}-response", "entry": [results[i] for i in range(len(entries))]}


# ---------------------------------------------------------------- operations
def everything(person_id: str, p: dict, request: Request) -> dict:
    pid = _check_person(p, person_id, request, "fhir.everything")
    base = _base(request)
    person = mpi.get(pid)
    out = [fhir_map.patient(person)]
    for rtype in store.RESOURCES:
        for row in store.list_for(rtype, pid, limit=5000):
            out.append(fhir_map.to_fhir(rtype, row))
    from pacs import index
    for s in index.query_studies({"x-person-id": pid}, limit=500):
        out.append(fhir_map.imaging_study(s, index.query_series({"StudyInstanceUID": s["StudyInstanceUID"]})))
    if any(r["resourceType"] == "ImagingStudy" for r in out):
        out.append(fhir_map.endpoint(facilities.local()))
    out.append(fhir_map.organization(facilities.local()))
    return _bundle("searchset", out, base, total=len(out))


def summary(person_id: str, p: dict, request: Request) -> dict:
    """IPS-style document: Composition + the resources its sections cite."""
    pid = _check_person(p, person_id, request, "fhir.summary")
    c = chart_mod.build(pid)
    sm = c["summary"]
    person = mpi.get(pid)
    resources = [fhir_map.patient(person), fhir_map.organization(facilities.local())]

    def sec(title: str, loinc: str, items: list[dict], rtype: str) -> dict:
        refs = []
        for it in items:
            row = store.get(rtype, it["id"]) if rtype != "imaging" else None
            if row:
                r = fhir_map.to_fhir(rtype, row)
                resources.append(r)
                refs.append({"reference": f"{r['resourceType']}/{r['id']}"})
        text = "".join(f"<li>{fhir_map._html(i.get('display') or i.get('text') or '')}</li>" for i in items) or "<li>None recorded</li>"  # noqa: SLF001
        s = {"title": title, "code": fhir_map.cc(fhir_map.LOINC, loinc, title),
             "text": {"status": "generated", "div": f"<div xmlns=\"http://www.w3.org/1999/xhtml\"><ul>{text}</ul></div>"}}
        if refs:
            s["entry"] = refs
        else:
            s["emptyReason"] = {"text": "No information"}
        return s
    sections = [sec("Problems", "11450-4", sm["active_problems"], "condition"),
                sec("Allergies and intolerances", "48765-2", sm["allergies"], "allergy"),
                sec("Medication summary", "10160-0", sm["active_medications"], "medication"),
                sec("Vital signs", "8716-3", sm["latest_vitals"], "observation"),
                sec("Results", "30954-2", sm["abnormal_labs"], "observation")]
    comp_id = uuid.uuid4().hex
    composition = {"resourceType": "Composition", "id": comp_id, "status": "final",
                   "type": fhir_map.cc(fhir_map.LOINC, "60591-5", "Patient summary Document"),
                   "subject": {"reference": f"Patient/{pid}"},
                   "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "author": [{"reference": f"Organization/{settings.facility_oid()}"}],
                   "title": f"Patient summary — {person['name']}", "section": sections}
    base = _base(request)
    seen: set[str] = set()
    entries = []
    for r in [composition] + resources:
        key = f"{r['resourceType']}/{r['id']}"
        if key in seen:
            continue
        seen.add(key)
        entries.append({"fullUrl": f"{base}/{key}", "resource": r})
    return {"resourceType": "Bundle", "id": uuid.uuid4().hex, "type": "document",
            "identifier": {"system": "urn:ietf:rfc:3986", "value": f"urn:uuid:{uuid.uuid4()}"},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "entry": entries}


def match(params: dict, p: dict) -> dict:
    """PDQm $match: Parameters{resource: Patient, onlyCertainMatches, count}."""
    res, only_certain, count = None, False, 10
    for prm in params.get("parameter") or []:
        if prm.get("name") == "resource":
            res = prm.get("resource")
        elif prm.get("name") == "onlyCertainMatches":
            only_certain = bool(prm.get("valueBoolean"))
        elif prm.get("name") == "count":
            count = int(prm.get("valueInteger") or 10)
    if not res or res.get("resourceType") != "Patient":
        raise FhirError(400, "invalid", "$match needs a Patient resource parameter")
    demo, idents = fhir_map.patient_in(res)
    hits: dict[str, float] = {}
    pid = mpi.find_by_any_identifier(idents)
    if pid:
        hits[pid] = 1.0
    for c in mpi.candidates(mpi.normalise_demographics(demo), limit=count):
        hits[c["person_id"]] = max(hits.get(c["person_id"], 0), c["score"])
    entries = []
    for person_id, score in sorted(hits.items(), key=lambda kv: -kv[1])[:count]:
        if score < settings.mpi_review_score() and score < 1.0:
            continue
        grade = "certain" if score >= 1.0 else "probable" if score >= settings.mpi_probable_score() else "possible"
        if only_certain and grade != "certain":
            continue
        if not access.decide(person_id, p)[0]:
            continue
        r = fhir_map.patient(mpi.get(person_id))
        entries.append({"fullUrl": f"Patient/{person_id}", "resource": r,
                        "search": {"mode": "match", "score": round(score, 4),
                                   "extension": [{"url": "http://hl7.org/fhir/StructureDefinition/match-grade",
                                                  "valueCode": grade}]}})
    audit.record("fhir.match", **pr.actor(p), detail={"results": len(entries), "kind": p.get("kind")})
    return {"resourceType": "Bundle", "id": uuid.uuid4().hex, "type": "searchset",
            "total": len(entries), "entry": entries}


def pix(source_identifier: str, target_systems: list[str], p: dict) -> dict:
    """PIXm $ihe-pix: all identifiers of the person holding *source_identifier*."""
    if "|" not in source_identifier:
        raise FhirError(400, "invalid", "sourceIdentifier must be system|value")
    system, value = source_identifier.split("|", 1)
    pid = mpi.find_by_identifier(system, value)
    if not pid:
        raise FhirError(404, "not-found", "sourceIdentifier not known")
    if not access.decide(pid, p)[0]:
        raise FhirError(403, "forbidden", "not permitted for this patient")
    person = mpi.get(pid)
    params = []
    for i in person["identifiers"]:
        if target_systems and i["system"] not in target_systems:
            continue
        if i["system"] == system and i["value"] == value:
            continue
        params.append({"name": "targetIdentifier", "valueIdentifier": {"system": i["system"], "value": i["value"]}})
    params.append({"name": "targetId", "valueReference": {"reference": f"Patient/{pid}"}})
    audit.record("fhir.pix", **pr.actor(p), resource=f"person:{pid}", detail={"kind": p.get("kind")})
    return {"resourceType": "Parameters", "parameter": params}


# ---------------------------------------------------------------- router
def make_router() -> APIRouter:
    r = APIRouter(prefix=prefix(), tags=["fhir"])

    def guard(fn):
        async def wrapper(*a, **kw):
            try:
                return await fn(*a, **kw)
            except FhirError as e:
                return outcome(e.status, e.code, e.diagnostics)
        wrapper.__name__ = fn.__name__
        wrapper.__signature__ = __import__("inspect").signature(fn)
        return wrapper

    async def _json(request: Request) -> dict:
        try:
            return json.loads(await request.body() or b"{}")
        except ValueError as e:
            raise FhirError(400, "structure", f"invalid JSON: {e}") from e

    @r.get("/metadata")
    async def metadata(request: Request):
        return _resp(capability(request))

    @r.post("")
    @r.post("/")
    @guard
    async def bundle(request: Request):
        p = _principal(request, "clinical.write")
        from starlette.concurrency import run_in_threadpool
        body = await _json(request)
        return _resp(await run_in_threadpool(process_bundle, body, p, request))

    @r.post("/Patient/$match")
    @guard
    async def patient_match(request: Request):
        p = _principal(request, "clinical.read")
        return _resp(match(await _json(request), p))

    @r.get("/Patient/$ihe-pix")
    @guard
    async def patient_pix(request: Request):
        p = _principal(request, "clinical.read")
        return _resp(pix(request.query_params.get("sourceIdentifier", ""),
                         request.query_params.getlist("targetSystem"), p))

    @r.get("/Patient/{pid}/$everything")
    @guard
    async def patient_everything(pid: str, request: Request):
        p = _principal(request, "clinical.read")
        from starlette.concurrency import run_in_threadpool
        return _resp(await run_in_threadpool(everything, pid, p, request))

    @r.get("/Patient/{pid}/$summary")
    @guard
    async def patient_summary(pid: str, request: Request):
        p = _principal(request, "clinical.read")
        from starlette.concurrency import run_in_threadpool
        return _resp(await run_in_threadpool(summary, pid, p, request))

    @r.get("/{rtype}")
    @guard
    async def search_type(rtype: str, request: Request):
        p = _principal(request, "clinical.read")
        params: dict[str, list[str]] = {}
        for k, v in request.query_params.multi_items():
            params.setdefault(k, []).append(v)
        from starlette.concurrency import run_in_threadpool
        return _resp(await run_in_threadpool(search, rtype, params, p, request))

    @r.get("/{rtype}/{rid}")
    @guard
    async def read_one(rtype: str, rid: str, request: Request):
        p = _principal(request, "clinical.read")
        res = read(rtype, rid, p, request)
        vid = (res.get("meta") or {}).get("versionId")
        return _resp(res, headers={"ETag": f'W/"{vid}"'} if vid else None)

    @r.get("/{rtype}/{rid}/_history")
    @guard
    async def history(rtype: str, rid: str, request: Request):
        p = _principal(request, "clinical.read")
        t = _rtype_for(rtype)
        cur = store.get(t, rid, include_deleted=True)
        if not cur:
            raise FhirError(404, "not-found", "not found")
        _check_person(p, cur["person_id"], request, "fhir.history")
        versions = [fhir_map.to_fhir(t, v) for v in store.history(t, rid)]
        b = _bundle("history", list(reversed(versions)), _base(request), total=len(versions))
        return _resp(b)

    @r.get("/{rtype}/{rid}/_history/{vid}")
    @guard
    async def vread(rtype: str, rid: str, vid: str, request: Request):
        p = _principal(request, "clinical.read")
        t = _rtype_for(rtype)
        cur = store.get(t, rid, include_deleted=True)
        if not cur:
            raise FhirError(404, "not-found", "not found")
        _check_person(p, cur["person_id"], request, "fhir.vread")
        for v in store.history(t, rid):
            if str(v["version"]) == vid:
                return _resp(fhir_map.to_fhir(t, v))
        raise FhirError(404, "not-found", f"version {vid} not found")

    @r.post("/{rtype}")
    @guard
    async def create_one(rtype: str, request: Request):
        p = _principal(request, "clinical.write")
        res, status = create(rtype, await _json(request), p, request)
        return _resp(res, status, headers={
            "Location": f"{_base(request)}/{res['resourceType']}/{res['id']}/_history/{(res.get('meta') or {}).get('versionId', '1')}"})

    @r.put("/{rtype}/{rid}")
    @guard
    async def update_one(rtype: str, rid: str, request: Request):
        p = _principal(request, "clinical.write")
        res, status = update(rtype, rid, await _json(request), p, request,
                             request.headers.get("if-match"))
        return _resp(res, status)

    @r.delete("/{rtype}/{rid}")
    @guard
    async def delete_one(rtype: str, rid: str, request: Request):
        p = _principal(request, "clinical.write")
        delete(rtype, rid, p, request)
        return Response(status_code=204)

    return r
