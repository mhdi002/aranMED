"""HTTP surface for the facility registry, MPI and interop capabilities."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import audit
import rbac
from clinicaldb import facilities, messages, migrate, mpi, settings

router = APIRouter(prefix="/api")

# Protocols other packages announce here when they load, so capabilities
# reflect what this build actually serves.
PROTOCOLS: dict[str, dict[str, Any]] = {}


def announce(name: str, **info: Any) -> None:
    PROTOCOLS[name] = info


def _ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


@router.get("/interop/capabilities")
async def capabilities() -> dict:
    """Public, PHI-free description a peer checks before exchanging data."""
    loc = facilities.local()
    return {
        "facility": {k: loc.get(k) for k in ("oid", "name", "kind", "ae_title",
                                             "dicom_port", "fhir_base", "dicomweb_base",
                                             "mllp_port", "base_url")},
        "schema": migrate.applied_versions(),
        "schema_expected": migrate.expected_versions(),
        "protocols": PROTOCOLS,
        "identifier_systems": {"mrn": settings.mrn_system(),
                               "national_id": settings.national_id_system()},
    }


# --- Facilities -------------------------------------------------------------
class FacilityIn(BaseModel):
    oid: str = Field(min_length=3, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    kind: str = "hospital"
    ae_title: Optional[str] = None
    dicom_host: Optional[str] = None
    dicom_port: Optional[int] = None
    base_url: Optional[str] = None
    fhir_base: Optional[str] = None
    dicomweb_base: Optional[str] = None
    mllp_host: Optional[str] = None
    mllp_port: Optional[int] = None
    auth_mode: str = "jwt"
    secret_env: Optional[str] = None
    trust_level: str = "peer"
    active: bool = True
    meta: Optional[dict] = None


@router.get("/facilities")
async def list_facilities(user: dict = Depends(rbac.require("clinical.read"))) -> dict:
    facilities.local()
    return {"facilities": facilities.list_all()}


@router.post("/facilities")
async def upsert_facility(body: FacilityIn, request: Request,
                          user: dict = Depends(rbac.require("interop.admin"))) -> dict:
    if body.oid == settings.facility_oid():
        raise HTTPException(400, "the local facility is configured through FACILITY_* settings")
    try:
        f = facilities.upsert(body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit.record("facility.upsert", actor=user, resource=f"facility:{body.oid}",
                 client_ip=_ip(request))
    return f


@router.delete("/facilities/{oid}")
async def delete_facility(oid: str, request: Request,
                          user: dict = Depends(rbac.require("interop.admin"))) -> dict:
    if not facilities.delete(oid):
        raise HTTPException(404, "facility not found (or is the local facility)")
    audit.record("facility.delete", actor=user, resource=f"facility:{oid}",
                 client_ip=_ip(request))
    return {"deleted": oid}


# --- MPI --------------------------------------------------------------------
class RegisterIn(BaseModel):
    demographics: dict = Field(default_factory=dict)
    identifiers: list[dict] = Field(default_factory=list)
    mrn: Optional[str] = None
    national_id: Optional[str] = None


@router.get("/mpi/search")
async def mpi_search(request: Request, q: Optional[str] = None,
                     birth_date: Optional[str] = None, identifier: Optional[str] = None,
                     sex: Optional[str] = None, limit: int = 50,
                     user: dict = Depends(rbac.require("clinical.read"))) -> dict:
    if not (q or birth_date or identifier):
        raise HTTPException(400, "give q (name), birth_date or identifier")
    hits = mpi.search(name=q, birth_date=birth_date, identifier=identifier or (q if q and any(ch.isdigit() for ch in q) else None),
                      sex=sex, limit=min(limit, 200))
    audit.record("mpi.search", actor=user, client_ip=_ip(request),
                 detail={"results": len(hits)})
    return {"results": hits}


@router.get("/mpi/links")
async def mpi_links(user: dict = Depends(rbac.require("clinical.write"))) -> dict:
    return {"links": mpi.pending_links()}


@router.post("/mpi/links/{link_id}/resolve")
async def mpi_resolve(link_id: str, accept: bool, request: Request,
                      user: dict = Depends(rbac.require("clinical.write"))) -> dict:
    try:
        out = mpi.resolve_link(link_id, accept=accept, actor=user.get("username"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit.record("mpi.link.resolve", actor=user, resource=f"mpi_link:{link_id}",
                 client_ip=_ip(request), detail={"accepted": accept})
    return out


@router.get("/mpi/{person_id}")
async def mpi_get(person_id: str, request: Request,
                  user: dict = Depends(rbac.require("clinical.read"))) -> dict:
    p = mpi.get(person_id)
    if not p:
        raise HTTPException(404, "person not found")
    audit.record("mpi.read", actor=user, resource=f"person:{p['id']}", client_ip=_ip(request))
    return p


@router.post("/mpi/register")
async def mpi_register(body: RegisterIn, request: Request,
                       user: dict = Depends(rbac.require("clinical.write"))) -> dict:
    idents = list(body.identifiers)
    if body.mrn:
        idents.append({"system": settings.mrn_system(), "value": body.mrn, "type": "MR",
                       "facility_oid": settings.facility_oid()})
    if body.national_id:
        idents.append({"system": settings.national_id_system(), "value": body.national_id,
                       "type": "NI"})
    out = mpi.register_person(body.demographics, idents)
    mrn = mpi.ensure_local_mrn(out["person_id"], body.mrn)
    audit.record("mpi.register", actor=user, resource=f"person:{out['person_id']}",
                 client_ip=_ip(request), detail={"outcome": out["outcome"]})
    return {**out, "mrn": mrn, "person": mpi.get(out["person_id"])}


class MergeIn(BaseModel):
    survivor_id: str
    merged_id: str
    reason: str = ""


@router.post("/mpi/merge")
async def mpi_merge(body: MergeIn, request: Request,
                    user: dict = Depends(rbac.require("clinical.write"))) -> dict:
    try:
        out = mpi.merge(body.survivor_id, body.merged_id, reason=body.reason,
                        actor=user.get("username"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit.record("mpi.merge", actor=user, resource=f"person:{out['survivor']}",
                 client_ip=_ip(request), detail={"merged": out["merged"]})
    return out


# --- Message log --------------------------------------------------------------
@router.get("/interop/messages")
async def interop_messages(protocol: Optional[str] = None, direction: Optional[str] = None,
                           status: Optional[str] = None, limit: int = 100, offset: int = 0,
                           user: dict = Depends(rbac.require("interop.admin"))) -> dict:
    return {"messages": messages.query(protocol=protocol, direction=direction, status=status,
                                       limit=min(limit, 500), offset=offset)}
