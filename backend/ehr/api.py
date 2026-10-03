"""``/api/clinical`` — the relational EHR for the UI, the agent and peers.

Reads go through :mod:`ehr.access` (restricted records need break-the-glass
locally; peers are subject to consent policy) and every PHI access is
audited with who, which person and why.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import audit
from clinicaldb import mpi, settings
from clinicaldb import principal as pr
from ehr import access, chart as chart_mod, store

router = APIRouter(prefix="/api/clinical", tags=["clinical"])
READ = Depends(pr.require("clinical.read"))
WRITE = Depends(pr.require("clinical.write"))


def _ip(r: Request) -> Optional[str]:
    return r.client.host if r.client else None


def _person_or_404(person_id: str) -> dict:
    p = mpi.get(person_id)
    if not p:
        raise HTTPException(404, "person not found")
    return p


def guard(person_id: str, principal: dict, request: Request, action: str) -> str:
    """Raise 403 unless *principal* may read *person_id*; audit either way."""
    allowed, why = access.decide(person_id, principal)
    audit.record(action, **pr.actor(principal), resource=f"person:{person_id}",
                 outcome="allow" if allowed else "deny", client_ip=_ip(request),
                 detail={"why": why, "kind": principal.get("kind"),
                         "purpose": principal.get("purpose")})
    if not allowed:
        raise HTTPException(403, why)
    return why


# ---------------------------------------------------------------- patients
@router.get("/patients")
def search_patients(request: Request, q: Optional[str] = None, identifier: Optional[str] = None,
                    birth_date: Optional[str] = None, scope: str = "local",
                    p: dict = READ) -> dict:
    if not (q or identifier or birth_date):
        raise HTTPException(400, "give q, identifier or birth_date")
    ident = identifier or (q if q and any(ch.isdigit() for ch in q) else None)
    local = mpi.search(name=q if not ident or q != ident else None, identifier=ident,
                       birth_date=birth_date)
    for h in local:
        h["location"] = "local"
        h["restricted"] = access.is_restricted(h["person_id"])
    remote, errors = [], []
    if scope in ("network", "all") and p.get("kind") == "user":
        from interop import federation
        res = federation.search_patients(q=q, identifier=ident, birth_date=birth_date,
                                         principal=p)
        remote, errors = res["results"], res["errors"]
    audit.record("clinical.search", **pr.actor(p), client_ip=_ip(request),
                 detail={"local": len(local), "remote": len(remote), "scope": scope})
    return {"results": local, "remote": remote, "errors": errors}


class PatientIn(BaseModel):
    demographics: dict = Field(default_factory=dict)
    mrn: Optional[str] = None
    national_id: Optional[str] = None
    identifiers: list[dict] = Field(default_factory=list)


@router.post("/patients")
def register_patient(body: PatientIn, request: Request, p: dict = WRITE) -> dict:
    idents = list(body.identifiers)
    if body.national_id:
        idents.append({"system": settings.national_id_system(), "value": body.national_id,
                       "type": "NI"})
    if body.mrn:
        idents.append({"system": settings.mrn_system(), "value": body.mrn, "type": "MR",
                       "facility_oid": settings.facility_oid()})
    reg = mpi.register_person(body.demographics, idents)
    mrn = mpi.ensure_local_mrn(reg["person_id"], body.mrn)
    audit.record("clinical.patient.register", **pr.actor(p), resource=f"person:{reg['person_id']}",
                 client_ip=_ip(request), detail={"outcome": reg["outcome"]})
    return {**reg, "mrn": mrn, "person": mpi.get(reg["person_id"])}


@router.get("/patients/{person_id}/chart")
def get_chart(person_id: str, request: Request, include_remote: bool = False,
              p: dict = READ) -> dict:
    person = _person_or_404(person_id)
    why = guard(person["id"], p, request, "clinical.chart.read")
    c = chart_mod.build(person["id"], principal=p,
                        include_remote=include_remote and p.get("kind") == "user")
    c["access"] = {"basis": why, "restricted": access.is_restricted(person["id"]),
                   "consents": access.consents(person["id"])}
    from ehr import transfers_view
    c["transfers"] = transfers_view.for_person(person["id"])
    return c


@router.get("/patients/{person_id}/timeline")
def get_timeline(person_id: str, request: Request, include_remote: bool = False,
                 p: dict = READ) -> dict:
    person = _person_or_404(person_id)
    guard(person["id"], p, request, "clinical.timeline.read")
    c = chart_mod.build(person["id"], principal=p,
                        include_remote=include_remote and p.get("kind") == "user")
    return {"events": chart_mod.timeline(c), "errors": c["errors"]}


class BreakGlassIn(BaseModel):
    reason: str
    minutes: Optional[int] = None


@router.post("/patients/{person_id}/break-glass")
def break_glass(person_id: str, body: BreakGlassIn, request: Request,
                p: dict = Depends(pr.require("clinical.breakglass"))) -> dict:
    person = _person_or_404(person_id)
    if p.get("kind") != "user":
        raise HTTPException(403, "break-the-glass is for signed-in clinicians")
    try:
        return access.break_glass(person["id"], p, body.reason, minutes=body.minutes)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ---------------------------------------------------------------- ask
class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    include_remote: bool = True


@router.post("/patients/{person_id}/ask")
async def ask(person_id: str, body: AskIn, request: Request, p: dict = READ) -> dict:
    """Question about this patient, answered by the agent over the full chart."""
    if p.get("kind") != "user":
        raise HTTPException(403, "available to signed-in users only")
    person = _person_or_404(person_id)
    guard(person["id"], p, request, "clinical.ask")
    from starlette.concurrency import run_in_threadpool
    c = await run_in_threadpool(chart_mod.build, person["id"], principal=p,
                                include_remote=body.include_remote)
    import app as app_mod
    text = (f"[Context: the user is viewing the clinical chart of person {person['id']}. "
            f"Chart summary follows; you may call clinical_* and pacs_* tools for more.]\n"
            f"{chart_mod.as_text(c)}\n\nQuestion: {body.question}")
    res = await app_mod.agent.run(session_id=f"chart:{p['id']}:{person['id']}", user_text=text,
                                  attachments={}, owner_user_id=p["id"])
    return {"answer": res.answer, "tool_calls": res.tool_calls,
            "critical_alerts": (res.state or {}).get("critical_alerts") or []}


# ---------------------------------------------------------------- resources
_TYPES = set(store.RESOURCES)


def _rtype(t: str) -> str:
    t = t.replace("-", "_")
    if t.endswith("ies"):
        t = t[:-3] + "y"
    elif t.endswith("s"):
        t = t[:-1]
    if t not in _TYPES:
        raise HTTPException(404, f"unknown resource type {t}")
    return t


@router.get("/patients/{person_id}/{rtype}")
def list_resources(person_id: str, rtype: str, request: Request, p: dict = READ) -> dict:
    t = _rtype(rtype)
    person = _person_or_404(person_id)
    guard(person["id"], p, request, f"clinical.{t}.read")
    items = store.list_for(t, person["id"])
    if t == "document":
        for i in items:
            i.pop("content", None)
    return {"items": items}


@router.post("/patients/{person_id}/{rtype}")
def create_resource(person_id: str, rtype: str, body: dict, request: Request,
                    p: dict = WRITE) -> dict:
    t = _rtype(rtype)
    person = _person_or_404(person_id)
    guard(person["id"], p, request, f"clinical.{t}.write")
    body = {k: v for k, v in body.items() if k not in ("id", "person_id", "facility_oid",
                                                      "version", "deleted")}
    try:
        item = store.create(t, {**body, "person_id": person["id"]}, actor=p.get("username"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit.record(f"clinical.{t}.create", **pr.actor(p), resource=f"person:{person['id']}",
                 client_ip=_ip(request), detail={"id": item["id"]})
    if t == "document":
        item.pop("content", None)
    return item


class PatchIn(BaseModel):
    changes: dict[str, Any]
    version: Optional[int] = None


@router.patch("/resources/{rtype}/{rid}")
def patch_resource(rtype: str, rid: str, body: PatchIn, request: Request,
                   p: dict = WRITE) -> dict:
    t = _rtype(rtype)
    cur = store.get(t, rid)
    if not cur:
        raise HTTPException(404, "not found")
    guard(cur["person_id"], p, request, f"clinical.{t}.write")
    try:
        item = store.update(t, rid, body.changes, actor=p.get("username"),
                            expected_version=body.version)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    if t == "document":
        item.pop("content", None)
    return item


@router.delete("/resources/{rtype}/{rid}")
def delete_resource(rtype: str, rid: str, request: Request, p: dict = WRITE) -> dict:
    t = _rtype(rtype)
    cur = store.get(t, rid)
    if not cur:
        raise HTTPException(404, "not found")
    guard(cur["person_id"], p, request, f"clinical.{t}.delete")
    store.delete(t, rid, actor=p.get("username"))
    return {"deleted": rid}


@router.get("/resources/{rtype}/{rid}/history")
def resource_history(rtype: str, rid: str, request: Request, p: dict = READ) -> dict:
    t = _rtype(rtype)
    cur = store.get(t, rid, include_deleted=True)
    if not cur:
        raise HTTPException(404, "not found")
    guard(cur["person_id"], p, request, f"clinical.{t}.history")
    return {"versions": store.history(t, rid)}


@router.get("/documents/{doc_id}")
def get_document(doc_id: str, request: Request, p: dict = READ) -> dict:
    d = store.get("document", doc_id)
    if not d:
        raise HTTPException(404, "document not found")
    guard(d["person_id"], p, request, "clinical.document.read")
    return d


