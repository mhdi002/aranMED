"""``/api/pacs`` — PACS management for the UI and the agent.

DICOMweb (``pacs.dicomweb``) is the standards-based data plane; this router
is the operational surface around it: browsing with friendly filters,
browser upload, node administration and connectivity tests, federated
search, retrieve/send jobs, worklist/MPPS, linked reports and statistics.
"""
from __future__ import annotations

import io
import zipfile
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

import audit
from clinicaldb import principal as pr
from pacs import config, dimse, federation, index, jobs, nodes, reports, worklist
from pacs.adapters import AdapterError, for_node

router = APIRouter(prefix="/api/pacs", tags=["pacs"])
READ = Depends(pr.require("pacs.read"))
WRITE = Depends(pr.require("pacs.write"))
ADMIN = Depends(pr.require("pacs.admin"))


def _ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def _audit(p: dict, action: str, resource: str, request: Request, **detail) -> None:
    audit.record(action, **pr.actor(p), resource=resource, client_ip=_ip(request),
                 detail=detail or None)


def _filters(q: Optional[str], patient_id: Optional[str], accession: Optional[str],
             modality: Optional[str], date_from: Optional[str], date_to: Optional[str],
             status: Optional[str], person_id: Optional[str],
             description: Optional[str]) -> dict[str, str]:
    f: dict[str, str] = {}
    if q:
        f["PatientName"] = q if ("*" in q or "?" in q) else f"*{q}*"
    if patient_id:
        f["PatientID"] = patient_id
    if accession:
        f["AccessionNumber"] = accession
    if modality:
        f["ModalitiesInStudy"] = modality
    if date_from or date_to:
        f["StudyDate"] = f"{(date_from or '').replace('-', '')}-{(date_to or '').replace('-', '')}"
    if status:
        f["x-status"] = status
    if person_id:
        f["x-person-id"] = person_id
    if description:
        f["StudyDescription"] = f"*{description}*"
    return f


# ---------------------------------------------------------------- studies
@router.get("/studies")
def list_studies(request: Request, q: Optional[str] = None, patient_id: Optional[str] = None,
                 accession: Optional[str] = None, modality: Optional[str] = None,
                 date_from: Optional[str] = None, date_to: Optional[str] = None,
                 status: Optional[str] = None, person_id: Optional[str] = None,
                 description: Optional[str] = None, scope: str = "local",
                 limit: int = 50, offset: int = 0, p: dict = READ) -> dict:
    f = _filters(q, patient_id, accession, modality, date_from, date_to, status, person_id,
                 description)
    limit = max(1, min(limit, 500))
    if scope == "local":
        studies = index.query_studies(f, limit=limit, offset=offset)
        out = {"studies": studies, "total": index.count_studies(f), "errors": []}
    else:
        remote_f = {k: v for k, v in f.items() if not k.startswith("x-")}
        res = federation.search(remote_f if scope == "remote" else f, scope=scope, limit=limit,
                                practitioner=p.get("username"))
        out = {"studies": res["studies"], "total": len(res["studies"]), "errors": res["errors"]}
    _audit(p, "pacs.search", "studies", request, scope=scope, results=len(out["studies"]))
    return out


@router.get("/studies/{study_uid}")
def study_detail(study_uid: str, request: Request, p: dict = READ) -> dict:
    s = index.get_study(study_uid)
    if not s:
        raise HTTPException(404, "study not found")
    series = index.query_series({"StudyInstanceUID": study_uid})
    for se in series:
        inst = index.query_instances({"SeriesInstanceUID": se["SeriesInstanceUID"]}, limit=5000)
        se["instances"] = [{k: i[k] for k in ("SOPInstanceUID", "InstanceNumber", "Rows",
                                              "Columns", "NumberOfFrames", "SOPClassUID")}
                           for i in inst]
    wl = worklist.get(s["AccessionNumber"]) if s.get("AccessionNumber") else None
    priors = []
    if s["_ext"].get("person_id"):
        priors = [x for x in index.query_studies({"x-person-id": s["_ext"]["person_id"]},
                                                 limit=50)
                  if x["StudyInstanceUID"] != study_uid]
    _audit(p, "pacs.study.read", f"study:{study_uid}", request)
    return {"study": s, "series": series, "reports": reports.list_for_study(study_uid),
            "worklist": wl, "priors": priors}


class StudyPatch(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None


@router.patch("/studies/{study_uid}")
def patch_study(study_uid: str, body: StudyPatch, request: Request, p: dict = WRITE) -> dict:
    if not index.get_study(study_uid):
        raise HTTPException(404, "study not found")
    import db
    allowed = ("received", "in_review", "read", "reported", "final", "archived")
    if body.status and body.status not in allowed:
        raise HTTPException(400, f"status must be one of {allowed}")
    with db.connect() as c:
        if body.status:
            c.execute("UPDATE pacs_studies SET status=?, updated_at=? WHERE study_uid=?",
                      (body.status, db.now(), study_uid))
        if body.priority is not None:
            c.execute("UPDATE pacs_studies SET priority=?, updated_at=? WHERE study_uid=?",
                      (body.priority or None, db.now(), study_uid))
    _audit(p, "pacs.study.update", f"study:{study_uid}", request,
           **body.model_dump(exclude_none=True))
    return index.get_study(study_uid)


@router.delete("/studies/{study_uid}")
def delete_study(study_uid: str, request: Request, p: dict = ADMIN) -> dict:
    n = index.delete_study(study_uid)
    if not n and not index.get_study(study_uid):
        raise HTTPException(404, "study not found")
    _audit(p, "pacs.study.delete", f"study:{study_uid}", request, instances=n)
    return {"deleted": study_uid, "instances": n}


@router.get("/studies/{study_uid}/verify")
def verify_study(study_uid: str, p: dict = READ) -> dict:
    return index.verify_study(study_uid)


@router.post("/upload")
async def upload(request: Request, files: list[UploadFile] = File(...), p: dict = WRITE) -> dict:
    """Browser upload: individual DICOM files and/or .zip archives of them."""
    blobs: list[tuple[str, bytes]] = []
    limit = config.max_upload_mb() * 1024 * 1024
    total = 0
    for f in files:
        data = await f.read()
        total += len(data)
        if total > limit:
            raise HTTPException(413, "upload too large")
        if data[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for name in z.namelist():
                    if not name.endswith("/"):
                        blobs.append((f"{f.filename}:{name}", z.read(name)))
        else:
            blobs.append((f.filename or "file", data))

    def work() -> dict:
        stored, dup, failed, studies = 0, 0, [], {}
        for name, data in blobs:
            try:
                out = index.ingest(data, source=f"upload:{p.get('username')}")
            except index.IngestError as e:
                failed.append({"file": name, "error": str(e)[:200]})
                continue
            studies[out["study_uid"]] = True
            if out["status"] == "duplicate":
                dup += 1
            else:
                stored += 1
        return {"stored": stored, "duplicates": dup, "failed": failed,
                "studies": [index.get_study(u) for u in studies]}

    res = await run_in_threadpool(work)
    _audit(p, "pacs.upload", "studies", request, stored=res["stored"],
           failed=len(res["failed"]))
    return res


# ---------------------------------------------------------------- reports
class ReportIn(BaseModel):
    text: str = Field(min_length=1)
    status: str = "draft"
    report_id: Optional[str] = None
    data: Optional[dict] = None
    source: str = "radiologist"


@router.get("/studies/{study_uid}/reports")
def get_reports(study_uid: str, p: dict = READ) -> dict:
    return {"reports": reports.list_for_study(study_uid)}


@router.post("/studies/{study_uid}/reports")
def save_report(study_uid: str, body: ReportIn, request: Request, p: dict = WRITE) -> dict:
    if body.status in ("final", "amended") and not pr.allows(p, "report.write"):
        raise HTTPException(403, "finalising a report requires report.write")
    try:
        rep = reports.save(study_uid, text=body.text, status=body.status, source=body.source,
                           author=p.get("username"), data=body.data, report_id=body.report_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _audit(p, "pacs.report.save", f"study:{study_uid}", request, status=body.status)
    return rep


# ---------------------------------------------------------------- AI
class AnalyzeIn(BaseModel):
    question: Optional[str] = None
    series_uid: Optional[str] = None
    max_images: int = 3
    template_id: Optional[str] = None


async def _run_tool(name: str, user: dict, **kwargs) -> dict:
    from registry import Registry
    from tools.base import ToolContext, registry as tool_registry
    import templates as templates_mod
    ctx = ToolContext(registry=Registry.get(), attachments={}, templates=templates_mod,
                      owner_user_id=user.get("id"))
    res = await tool_registry.get(name).run(ctx, **kwargs)
    if res.error:
        raise HTTPException(422, res.error)
    return {"content": res.content, "data": res.data}


@router.post("/studies/{study_uid}/analyze")
async def analyze_study(study_uid: str, body: AnalyzeIn,
                        p: dict = Depends(pr.require("pacs.read"))) -> dict:
    """AI draft read of a study (vision findings -> templated DRAFT report)."""
    if p.get("kind") != "user":
        raise HTTPException(403, "AI analysis is available to signed-in users only")
    return await _run_tool("pacs_analyze_study", p, study_uid=study_uid,
                           question=body.question, series_uid=body.series_uid,
                           max_images=body.max_images, template_id=body.template_id)


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


@router.post("/studies/{study_uid}/ask")
async def ask_about_study(study_uid: str, body: AskIn,
                          p: dict = Depends(pr.require("pacs.read"))) -> dict:
    """Chat with the agent about one study (it can call any pacs_* tool)."""
    if p.get("kind") != "user":
        raise HTTPException(403, "available to signed-in users only")
    if not index.get_study(study_uid):
        raise HTTPException(404, "study not found")
    import app as app_mod
    text = (f"[Context: the user is viewing imaging study {study_uid} in the PACS viewer.]\n"
            f"{body.question}")
    res = await app_mod.agent.run(session_id=f"pacs:{p['id']}:{study_uid}", user_text=text,
                                  attachments={}, owner_user_id=p["id"])
    return {"answer": res.answer, "tool_calls": res.tool_calls, "model": res.model,
            "critical_alerts": (res.state or {}).get("critical_alerts") or []}


# ---------------------------------------------------------------- nodes
class NodeIn(BaseModel):
    name: str
    kind: str = "dimse"
    ae_title: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    base_url: Optional[str] = None
    auth_env: Optional[str] = None
    facility_oid: Optional[str] = None
    issuer: Optional[str] = None
    allow_store: bool = True
    allow_query: bool = True
    allow_retrieve: bool = True
    is_move_destination: bool = True
    federate: bool = False
    active: bool = True


@router.get("/nodes")
def list_nodes(p: dict = READ) -> dict:
    return {"nodes": nodes.list_nodes(), "local": dimse.status(),
            "federation_targets": [{"id": t["id"], "name": t["name"], "kind": t["kind"]}
                                   for t in federation.remote_targets()]}


@router.post("/nodes")
def create_node(body: NodeIn, request: Request, p: dict = ADMIN) -> dict:
    try:
        n = nodes.save(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _audit(p, "pacs.node.create", f"node:{n['id']}", request, ae=n.get("ae_title"))
    return n


@router.put("/nodes/{node_id}")
def update_node(node_id: str, body: NodeIn, request: Request, p: dict = ADMIN) -> dict:
    if not nodes.get(node_id):
        raise HTTPException(404, "node not found")
    try:
        n = nodes.save(body.model_dump(), node_id=node_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _audit(p, "pacs.node.update", f"node:{node_id}", request)
    return n


@router.delete("/nodes/{node_id}")
def delete_node(node_id: str, request: Request, p: dict = ADMIN) -> dict:
    if not nodes.delete(node_id):
        raise HTTPException(404, "node not found")
    _audit(p, "pacs.node.delete", f"node:{node_id}", request)
    return {"deleted": node_id}


@router.post("/nodes/{node_id}/echo")
def echo_node(node_id: str, p: dict = READ) -> dict:
    node = federation.resolve_target(node_id)
    if not node:
        raise HTTPException(404, "node not found")
    ok = for_node(node).echo()
    if not node_id.startswith("facility:"):
        nodes.record_echo(node_id, ok)
    return {"ok": ok}


class RemoteQuery(BaseModel):
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = 100


@router.post("/nodes/{node_id}/query")
def query_node(node_id: str, body: RemoteQuery, request: Request, p: dict = READ) -> dict:
    node = federation.resolve_target(node_id)
    if not node:
        raise HTTPException(404, "node not found")
    try:
        rows = for_node({**node, "_practitioner": p.get("username")}).search_studies(
            body.filters, limit=body.limit)
    except AdapterError as e:
        raise HTTPException(502, str(e)) from e
    _audit(p, "pacs.remote.query", f"node:{node_id}", request, results=len(rows))
    return {"studies": rows}


class TransferIn(BaseModel):
    study_uid: str
    node_id: str
    series_uid: Optional[str] = None
    wait: bool = False
    purpose: str = "TREAT"


@router.post("/retrieve")
def retrieve(body: TransferIn, request: Request, p: dict = WRITE) -> dict:
    if not federation.resolve_target(body.node_id):
        raise HTTPException(404, "node not found")
    job = jobs.submit("retrieve", target=body.node_id, study_uid=body.study_uid,
                      params={"series_uid": body.series_uid, "practitioner": p.get("username"),
                              "purpose": body.purpose},
                      created_by=p.get("username"), wait=body.wait)
    _audit(p, "pacs.retrieve", f"study:{body.study_uid}", request, node=body.node_id)
    return job


@router.post("/send")
def send(body: TransferIn, request: Request, p: dict = ADMIN) -> dict:
    if not index.get_study(body.study_uid):
        raise HTTPException(404, "study not found")
    if not federation.resolve_target(body.node_id):
        raise HTTPException(404, "node not found")
    job = jobs.submit("send", target=body.node_id, study_uid=body.study_uid,
                      params={"practitioner": p.get("username"), "purpose": body.purpose},
                      created_by=p.get("username"), wait=body.wait)
    _audit(p, "pacs.send", f"study:{body.study_uid}", request, node=body.node_id)
    return job


@router.get("/jobs")
def list_jobs(status: Optional[str] = None, limit: int = 100, p: dict = READ) -> dict:
    return {"jobs": jobs.list_jobs(limit=min(limit, 500), status=status)}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, p: dict = READ) -> dict:
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return j


# ---------------------------------------------------------------- worklist
class WorklistIn(BaseModel):
    modality: str
    patient_name: Optional[str] = None
    patient_id: Optional[str] = None
    issuer: Optional[str] = None
    patient_birth_date: Optional[str] = None
    patient_sex: Optional[str] = None
    person_id: Optional[str] = None
    accession: Optional[str] = None
    station_ae: Optional[str] = None
    station_name: Optional[str] = None
    scheduled_start: Optional[str] = None
    procedure_code: Optional[str] = None
    procedure_description: Optional[str] = None
    referring_physician: Optional[str] = None
    reason: Optional[str] = None
    priority: Optional[str] = None
    order_id: Optional[str] = None


@router.get("/worklist")
def list_worklist(status: Optional[str] = None, modality: Optional[str] = None,
                  date: Optional[str] = None, person_id: Optional[str] = None,
                  p: dict = READ) -> dict:
    return {"items": worklist.list_entries(status=status, modality=modality, date=date,
                                           person_id=person_id)}


@router.post("/worklist")
def create_worklist(body: WorklistIn, request: Request, p: dict = WRITE) -> dict:
    try:
        wl = worklist.create({**body.model_dump(exclude_none=True), "source": "api"})
    except Exception as e:  # noqa: BLE001  (unique accession, validation)
        raise HTTPException(400, str(e)) from e
    _audit(p, "pacs.worklist.create", f"accession:{wl['accession']}", request)
    return wl


class WorklistPatch(BaseModel):
    status: Optional[str] = None
    scheduled_start: Optional[str] = None
    station_ae: Optional[str] = None
    priority: Optional[str] = None


@router.patch("/worklist/{wid}")
def patch_worklist(wid: str, body: WorklistPatch, request: Request, p: dict = WRITE) -> dict:
    try:
        wl = worklist.update(wid, body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not wl:
        raise HTTPException(404, "worklist entry not found")
    _audit(p, "pacs.worklist.update", f"accession:{wl['accession']}", request)
    return wl


@router.get("/mpps")
def list_mpps(p: dict = READ) -> dict:
    return {"items": worklist.list_mpps()}


# ---------------------------------------------------------------- status
@router.get("/stats")
def stats(p: dict = READ) -> dict:
    return {**index.stats(), "dimse": dimse.status(),
            "worklist": {s: len(worklist.list_entries(status=s))
                         for s in ("scheduled", "in_progress")},
            "jobs": {s: len(jobs.list_jobs(status=s)) for s in ("queued", "running", "failed")}}
