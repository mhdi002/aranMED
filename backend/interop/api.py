"""HTTP surface for transfers, EMS and the interop utilities."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

import audit
from clinicaldb import facilities, mpi, settings
from clinicaldb import principal as pr
from interop import cda, ems, federation, hl7_handlers, mllp, transfers

router = APIRouter(tags=["interop"])


def _ip(r: Request) -> Optional[str]:
    return r.client.host if r.client else None


def _user_only(p: dict) -> None:
    if p.get("kind") != "user":
        raise HTTPException(403, "this action is for signed-in users of this hospital")


def _peer_only(p: dict) -> None:
    if p.get("kind") != "peer":
        raise HTTPException(403, "this endpoint is for peer facilities")


# ================================================================ transfers
class TransferIn(BaseModel):
    person_id: str
    to_facility: str
    urgency: str = "routine"
    reason: str = ""
    clinical_summary: str = ""
    transport_mode: str = ""
    ems_unit: str = ""
    include_imaging: bool = True


@router.get("/api/transfers")
def list_transfers(direction: Optional[str] = None, status: Optional[str] = None,
                   p: dict = Depends(pr.require("transfer.read"))) -> dict:
    return {"transfers": transfers.list_all(direction=direction, status=status),
            "peers": [{"oid": f["oid"], "name": f["name"]} for f in facilities.peers()
                      if f.get("base_url")]}


@router.post("/api/transfers")
def create_transfer(body: TransferIn, request: Request,
                    p: dict = Depends(pr.require("transfer.write"))) -> dict:
    _user_only(p)
    try:
        return transfers.request(body.person_id, body.to_facility, user=p, urgency=body.urgency,
                                 reason=body.reason, clinical_summary=body.clinical_summary,
                                 transport_mode=body.transport_mode, ems_unit=body.ems_unit,
                                 include_imaging=body.include_imaging)
    except (transfers.TransferError, PermissionError) as e:
        raise HTTPException(400, str(e)) from e


@router.get("/api/transfers/{tid}")
def get_transfer(tid: str, p: dict = Depends(pr.require("transfer.read"))) -> dict:
    t = transfers.get(tid)
    if not t:
        raise HTTPException(404, "transfer not found")
    return t


class StatusIn(BaseModel):
    note: str = ""
    eta: Optional[str] = None
    reason: Optional[str] = None


def _step(tid: str, status: str, body: StatusIn, p: dict) -> dict:
    _user_only(p)
    try:
        return transfers.change_status(tid, status, user=p, note=body.note, eta=body.eta,
                                       rejection_reason=body.reason)
    except transfers.TransferError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/api/transfers/{tid}/accept")
def accept(tid: str, body: StatusIn, p: dict = Depends(pr.require("transfer.write"))) -> dict:
    return _step(tid, "accepted", body, p)


@router.post("/api/transfers/{tid}/reject")
def reject(tid: str, body: StatusIn, p: dict = Depends(pr.require("transfer.write"))) -> dict:
    return _step(tid, "rejected", body, p)


@router.post("/api/transfers/{tid}/cancel")
def cancel(tid: str, body: StatusIn, p: dict = Depends(pr.require("transfer.write"))) -> dict:
    return _step(tid, "cancelled", body, p)


@router.post("/api/transfers/{tid}/depart")
def depart(tid: str, body: StatusIn, p: dict = Depends(pr.require("transfer.write"))) -> dict:
    return _step(tid, "in_transit", body, p)


@router.post("/api/transfers/{tid}/arrive")
def arrive(tid: str, body: StatusIn, p: dict = Depends(pr.require("transfer.write"))) -> dict:
    return _step(tid, "arrived", body, p)


@router.post("/api/transfers/{tid}/complete")
def complete(tid: str, body: StatusIn, p: dict = Depends(pr.require("transfer.write"))) -> dict:
    return _step(tid, "completed", body, p)


@router.post("/api/transfers/{tid}/resend-package")
def resend(tid: str, p: dict = Depends(pr.require("transfer.write"))) -> dict:
    _user_only(p)
    t = transfers.get(tid)
    if not t or t["direction"] != "outgoing":
        raise HTTPException(404, "outgoing transfer not found")
    try:
        return transfers.send_package(tid, practitioner=p["username"])
    except transfers.TransferError as e:
        raise HTTPException(502, str(e)) from e


# --- peer-facing (called by the other hospital) ---------------------------------
@router.post("/api/transfers/inbound")
def inbound(body: dict, p: dict = Depends(pr.require("transfer.write"))) -> dict:
    _peer_only(p)
    if not body.get("transfer_id") or not body.get("patient"):
        raise HTTPException(400, "transfer_id and patient are required")
    t = transfers.receive_request(body, p)
    audit.record("transfer.inbound", **pr.actor(p), resource=f"person:{t['person_id']}",
                 detail={"from": p["facility_oid"], "remote": body["transfer_id"]})
    return t


@router.post("/api/transfers/inbound/{remote_id}/status")
def inbound_status(remote_id: str, body: dict, p: dict = Depends(pr.require("transfer.write"))) -> dict:
    _peer_only(p)
    try:
        return transfers.receive_status(remote_id, body, p)
    except transfers.TransferError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/api/transfers/inbound/{remote_id}/package")
def inbound_package(remote_id: str, body: dict, request: Request,
                    p: dict = Depends(pr.require("transfer.write"))) -> dict:
    _peer_only(p)
    from interop.fhir_server import FhirError
    try:
        return transfers.receive_package(remote_id, body, p, request)
    except transfers.TransferError as e:
        raise HTTPException(409, str(e)) from e
    except FhirError as e:
        raise HTTPException(e.status, e.diagnostics) from e


# ================================================================ EMS
@router.post("/api/ems/notify")
async def ems_notify(request: Request, p: dict = Depends(pr.require("ems.write"))) -> dict:
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "empty report")
    from starlette.concurrency import run_in_threadpool
    try:
        out = await run_in_threadpool(
            ems.ingest, raw, content_type=request.headers.get("content-type", "application/json"),
            source_facility=p.get("facility_oid") if p.get("kind") == "peer" else None,
            actor=p.get("username"))
    except (ValueError, KeyError) as e:
        raise HTTPException(422, f"could not parse EMS report: {e}") from e
    audit.record("ems.notify", **pr.actor(p), resource=f"person:{out['person_id']}",
                 client_ip=_ip(request), detail={"kind": p.get("kind")})
    return out


@router.get("/api/ems/board")
def ems_board(status: Optional[str] = None, p: dict = Depends(pr.require("ems.read"))) -> dict:
    return {"notifications": ems.board(status=status)}


@router.get("/api/ems/{nid}")
def ems_get(nid: str, p: dict = Depends(pr.require("ems.read"))) -> dict:
    n = ems.get(nid)
    if not n:
        raise HTTPException(404, "not found")
    return n


@router.post("/api/ems/{nid}/{status}")
def ems_status(nid: str, status: str, request: Request,
               p: dict = Depends(pr.require("ems.write"))) -> dict:
    _user_only(p)
    try:
        n = ems.set_status(nid, status, p)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except KeyError as e:
        raise HTTPException(404, "not found") from e
    audit.record(f"ems.{status}", **pr.actor(p), resource=f"person:{n['person_id']}",
                 client_ip=_ip(request))
    return n


# ================================================================ interop utils
@router.post("/api/interop/hl7")
async def hl7_http(request: Request, p: dict = Depends(pr.require("clinical.write"))) -> Response:
    """HL7 v2 over HTTP (for systems that cannot do MLLP); returns the ACK."""
    raw = (await request.body()).decode("utf-8", errors="replace")
    from starlette.concurrency import run_in_threadpool
    ack = await run_in_threadpool(hl7_handlers.process, raw, peer=f"http:{p.get('username')}")
    return Response(ack, media_type="x-application/hl7-v2+er7")


class Hl7SendIn(BaseModel):
    facility_oid: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    message: str


@router.post("/api/interop/hl7/send")
def hl7_send(body: Hl7SendIn, p: dict = Depends(pr.require("interop.admin"))) -> dict:
    host, port = body.host, body.port
    if body.facility_oid:
        f = facilities.get_by_oid(body.facility_oid)
        if not f or not f.get("mllp_host"):
            raise HTTPException(404, "facility has no MLLP endpoint")
        host, port = f["mllp_host"], f["mllp_port"]
    if not host or not port:
        raise HTTPException(400, "give facility_oid or host/port")
    try:
        ack = mllp.send(host, int(port), body.message.replace("\n", "\r"))
    except OSError as e:
        raise HTTPException(502, f"MLLP send failed: {e}") from e
    return {"ack": ack, "accepted": "MSA|AA" in ack}


@router.get("/api/interop/cda/{person_id}")
def cda_export(person_id: str, request: Request,
               p: dict = Depends(pr.require("clinical.read"))) -> Response:
    from ehr.api import guard
    person = mpi.get(person_id)
    if not person:
        raise HTTPException(404, "person not found")
    guard(person["id"], p, request, "clinical.cda.export")
    return Response(cda.export_ccd(person["id"]), media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="ccd-{person["id"]}.xml"'})


@router.post("/api/interop/cda")
async def cda_import(request: Request, p: dict = Depends(pr.require("clinical.write"))) -> dict:
    raw = await request.body()
    try:
        out = cda.import_ccd(raw, actor=p.get("username"),
                             source_facility=p.get("facility_oid") if p.get("kind") == "peer" else None)
    except (ValueError, Exception) as e:  # noqa: BLE001  (lxml syntax errors etc.)
        raise HTTPException(422, f"could not import CDA: {e}") from e
    audit.record("clinical.cda.import", **pr.actor(p), resource=f"person:{out['person_id']}")
    return out


@router.get("/api/interop/discover/{person_id}")
def discover(person_id: str, request: Request, purpose: str = "TREAT",
             p: dict = Depends(pr.require("clinical.read"))) -> dict:
    _user_only(p)
    person = mpi.get(person_id)
    if not person:
        raise HTTPException(404, "person not found")
    if purpose not in ("TREAT", "ETREAT"):
        raise HTTPException(400, "purpose must be TREAT or ETREAT")
    if purpose == "ETREAT" and not pr.allows(p, "clinical.breakglass"):
        raise HTTPException(403, "emergency access requires clinical.breakglass")
    res = federation.discover(person, p, purpose)
    audit.record("clinical.discover", **pr.actor(p), resource=f"person:{person['id']}",
                 client_ip=_ip(request), detail={"purpose": purpose, "found": len(res["matches"])})
    return res


@router.get("/api/interop/status")
def interop_status(p: dict = Depends(pr.require("clinical.read"))) -> dict:
    from interop import fhir_server
    from pacs import dimse
    return {"facility": facilities.local(), "mllp": mllp.status(), "dimse": dimse.status(),
            "fhir_base": (settings.public_base_url() or "") + fhir_server.prefix(),
            "peers": [{**{k: f.get(k) for k in ("oid", "name", "kind", "fhir_base", "dicomweb_base",
                                                 "mllp_host", "mllp_port", "trust_level", "active")},
                       "has_secret": f.get("has_secret")} for f in facilities.peers()]}


class PeerTestIn(BaseModel):
    oid: str


@router.post("/api/interop/peers/test")
def peer_test(body: PeerTestIn, p: dict = Depends(pr.require("interop.admin"))) -> dict:
    """Check reachability, schema compatibility and token acceptance of a peer."""
    import httpx
    f = facilities.get_by_oid(body.oid)
    if not f or not f.get("base_url"):
        raise HTTPException(404, "peer has no base_url")
    out: dict[str, Any] = {"oid": f["oid"], "name": f["name"]}
    try:
        caps = httpx.get(f["base_url"].rstrip("/") + "/api/interop/capabilities", timeout=10).json()
        from clinicaldb import migrate
        mine = migrate.applied_versions()
        out["reachable"] = True
        out["schema_compatible"] = all(caps["schema"].get(k) == v for k, v in mine.items())
        out["schema"] = {"local": mine, "peer": caps["schema"]}
        out["oid_matches"] = caps["facility"]["oid"] == f["oid"]
    except Exception as e:  # noqa: BLE001
        out.update(reachable=False, error=str(e)[:200])
        return out
    if f.get("fhir_base"):
        try:
            with federation.client(f, p) as c:
                r = c.get("/metadata")
                out["fhir"] = r.status_code == 200
                r = c.get("/Patient", params={"identifier": "urn:aranmed:probe|none"})
                out["token_accepted"] = r.status_code == 200
        except Exception as e:  # noqa: BLE001
            out["token_accepted"] = False
            out["error"] = str(e)[:200]
    return out
