"""Clinical federation: find a patient at peer hospitals and read their record.

When a patient arrives somewhere they have never been, the clinician still
needs their history. ``discover`` locates the person at every active peer
facility that publishes a FHIR base URL — by shared identifiers first (the
national id, then any MRN issued by that peer), falling back to IHE PDQm
``$match`` on demographics accepting only certain/probable matches — and
``remote_chart`` pulls ``Patient/$everything`` from each, mapping resources
back into chart items tagged with their source facility.

All calls carry a signed peer token naming the practitioner and purpose of
use; the peer applies its own consent policy and audits the access. Peer
failures degrade the answer (``errors``) instead of failing it.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
from typing import Any, Optional

import httpx

from clinicaldb import facilities, messages, mpi, settings
from clinicaldb import principal as pr
from interop import fhir_map

log = logging.getLogger("interop.federation")


def _timeout() -> float:
    return settings.env_float("FEDERATION_TIMEOUT_SEC", 20.0)


def fhir_peers() -> list[dict]:
    return [f for f in facilities.peers() if f.get("fhir_base") and facilities.peer_secret(f)]


def client(f: dict, principal: Optional[dict], purpose: str = "TREAT") -> httpx.Client:
    who = (principal or {}).get("username") or "system"
    token = pr.token_for_peer(f, practitioner=who, purpose=purpose)
    return httpx.Client(base_url=f["fhir_base"].rstrip("/"), timeout=_timeout(),
                        headers={"Authorization": f"Bearer {token}",
                                 "Accept": "application/fhir+json"})


def _parallel(fn, peers: list[dict]) -> tuple[list[Any], list[dict]]:
    results, errors = [], []
    if not peers:
        return results, errors
    with cf.ThreadPoolExecutor(max_workers=min(8, len(peers))) as ex:
        futs = {ex.submit(fn, f): f for f in peers}
        for fut in cf.as_completed(futs):
            f = futs[fut]
            try:
                r = fut.result()
                if r is not None:
                    results.append(r)
            except Exception as e:  # noqa: BLE001
                errors.append({"facility": f["name"], "facility_oid": f["oid"], "error": str(e)[:300]})
    return results, errors


def _remote_patient_summary(f: dict, res: dict) -> dict:
    demo, idents = fhir_map.patient_in(res)
    return {"facility_oid": f["oid"], "facility": f["name"], "remote_id": res["id"],
            "name": " ".join(x for x in (demo.get("given"), demo.get("family")) if x) or "(unnamed)",
            "demographics": {k: v for k, v in demo.items() if v}, "identifiers": idents,
            "location": "remote"}


def search_patients(*, q: Optional[str] = None, identifier: Optional[str] = None,
                    birth_date: Optional[str] = None, principal: Optional[dict] = None,
                    purpose: str = "TREAT") -> dict:
    params: dict[str, str] = {}
    if identifier:
        params["identifier"] = identifier
    if q and q != identifier:
        params["name"] = q
    if birth_date:
        params["birthdate"] = birth_date

    def one(f: dict):
        with client(f, principal, purpose) as c:
            r = c.get("/Patient", params=params)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        messages.log(direction="out", protocol="fhir", message_type="Patient?search",
                     peer=f["oid"], status="ok")
        return [_remote_patient_summary(f, e["resource"]) for e in r.json().get("entry") or []
                if e["resource"].get("resourceType") == "Patient"]
    lists, errors = _parallel(one, fhir_peers())
    return {"results": [x for lst in lists for x in lst], "errors": errors}


def _find_at_peer(f: dict, person: dict, principal: Optional[dict], purpose: str) -> Optional[dict]:
    idents = person["identifiers"]
    nat = [i for i in idents if i["system"] == settings.national_id_system()]
    theirs = [i for i in idents if i["system"] == settings.mrn_system(f["oid"])]
    others = [i for i in idents if i not in nat and i not in theirs
              and not i["system"].startswith("urn:aranmed:legacy")]
    with client(f, principal, purpose) as c:
        for i in nat + theirs + others:
            r = c.get("/Patient", params={"identifier": f"{i['system']}|{i['value']}"})
            if r.status_code == 200:
                all_entries = r.json().get("entry") or []
                entries = [e for e in all_entries if e["resource"].get("resourceType") == "Patient"]
                withheld = [e["resource"] for e in all_entries
                            if e["resource"].get("resourceType") == "OperationOutcome"]
                if withheld and not entries:
                    raise RuntimeError(withheld[0]["issue"][0].get("diagnostics") or "withheld by consent")
                if len(entries) == 1:
                    return {**_remote_patient_summary(f, entries[0]["resource"]),
                            "matched_on": f"identifier {i['system']}"}
            elif r.status_code in (401, 403):
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        body = {"resourceType": "Parameters", "parameter": [
            {"name": "resource", "resource": fhir_map.patient(person)},
            {"name": "count", "valueInteger": 3}]}
        r = c.post("/Patient/$match", json=body)
    if r.status_code != 200:
        return None
    for e in r.json().get("entry") or []:
        grade = next((x.get("valueCode") for x in (e.get("search") or {}).get("extension") or []), None)
        if grade in ("certain", "probable"):
            return {**_remote_patient_summary(f, e["resource"]),
                    "matched_on": f"$match ({grade}, {e['search'].get('score')})"}
    return None


def discover(person: dict, principal: Optional[dict] = None, purpose: str = "TREAT") -> dict:
    found, errors = _parallel(lambda f: _find_at_peer(f, person, principal, purpose), fhir_peers())
    for m in found:
        if (m.get("matched_on") or "").startswith("identifier"):
            # A deterministic match (shared identifier) makes the peer's other
            # identifiers ours too — e.g. its MRN, so imaging it sends later
            # lands on the same person. Probabilistic ($match) results are
            # shown but never linked automatically.
            for i in m.get("identifiers") or []:
                mpi.add_identifier(person["id"], i["system"], i["value"],
                                   type_=i.get("type") or "MR", facility_oid=i.get("facility_oid"))
    return {"matches": found, "errors": errors}


def fetch_everything(f: dict, remote_id: str, principal: Optional[dict],
                     purpose: str = "TREAT") -> list[dict]:
    with client(f, principal, purpose) as c:
        r = c.get(f"/Patient/{remote_id}/$everything")
    if r.status_code != 200:
        raise RuntimeError(f"$everything HTTP {r.status_code}: {r.text[:200]}")
    messages.log(direction="out", protocol="fhir", message_type="Patient/$everything",
                 peer=f["oid"], status="ok")
    return [e["resource"] for e in r.json().get("entry") or []]


def fetch_document(person: dict, facility_oid: str, remote_doc_id: str, principal: Optional[dict],
                   purpose: str = "TREAT") -> dict:
    """Full content of one document held at a peer, read on demand.

    The peer applies its own consent rules and audits the read; we only
    accept the document if it belongs to the person matched at that peer.
    """
    f = facilities.get_by_oid(facility_oid)
    if not f or f.get("is_local") or not f.get("fhir_base"):
        raise LookupError("unknown peer facility")
    match = _find_at_peer(f, person, principal, purpose)
    if not match:
        raise LookupError("patient not found at that facility")
    with client(f, principal, purpose) as c:
        r = c.get(f"/DocumentReference/{remote_doc_id}")
    if r.status_code in (401, 403):
        raise PermissionError(f"HTTP {r.status_code}: {r.text[:200]}")
    if r.status_code != 200:
        raise LookupError(f"document not available (HTTP {r.status_code})")
    res = r.json()
    if fhir_map._ref_id(res.get("subject"), "Patient") != match["remote_id"]:  # noqa: SLF001
        raise LookupError("document belongs to another patient")
    _, vals = fhir_map.from_fhir(res)
    messages.log(direction="out", protocol="fhir", message_type="DocumentReference",
                 peer=f["oid"], status="ok")
    src = vals.get("source_facility") or f["oid"]
    return {**vals, "id": f"{f['oid']}:{remote_doc_id}", "remote_id": remote_doc_id,
            "person_id": person["id"],
            "source": {"facility_oid": src, "facility": (facilities.get_by_oid(src) or {}).get("name", src),
                       "held": "remote", "remote": True, "via": f["oid"]}}


def _study_from_fhir(res: dict, f: dict) -> dict:
    started = (res.get("started") or "").replace("-", "")[:8]
    return {"StudyInstanceUID": res["id"], "StudyDate": started,
            "StudyDescription": res.get("description"),
            "ModalitiesInStudy": [m.get("code") for m in res.get("modality") or []],
            "NumberOfStudyRelatedSeries": res.get("numberOfSeries"),
            "NumberOfStudyRelatedInstances": res.get("numberOfInstances"),
            "AccessionNumber": next((i.get("value") for i in res.get("identifier") or []
                                     if ((i.get("type") or {}).get("coding") or [{}])[0].get("code") == "ACSN"), None),
            "_ext": {"origin_facility": fhir_map._parse_source(res)[0] or f["oid"]},  # noqa: SLF001
            "locations": [{"kind": "dicomweb", "id": f"facility:{f['oid']}", "name": f["name"],
                           "facility_oid": f["oid"]}],
            "source": {"facility_oid": f["oid"], "facility": f["name"], "held": "remote",
                       "remote": True}}


def remote_chart(person: dict, principal: dict) -> dict:
    """Chart remote source (registered with ehr.chart)."""
    purpose = principal.get("chart_purpose") or "TREAT"
    disc = discover(person, principal, purpose)
    items: dict[str, list[dict]] = {}
    imaging: list[dict] = []
    errors = list(disc["errors"])
    fac_oids = []
    for m in disc["matches"]:
        f = facilities.get_by_oid(m["facility_oid"])
        try:
            resources = fetch_everything(f, m["remote_id"], principal, purpose)
        except Exception as e:  # noqa: BLE001
            errors.append({"facility": f["name"], "facility_oid": f["oid"], "error": str(e)[:300]})
            continue
        fac_oids.append(f["oid"])
        for res in resources:
            rt = res.get("resourceType")
            if rt == "ImagingStudy":
                imaging.append(_study_from_fhir(res, f))
                continue
            if rt not in fhir_map.FHIR_TO_TYPE:
                continue
            try:
                rtype, vals = fhir_map.from_fhir(res)
            except Exception:  # noqa: BLE001
                continue
            src_fac = vals.get("source_facility") or f["oid"]
            vals.update({"id": f"{f['oid']}:{res['id']}", "resource_type": rtype,
                         "person_id": person["id"], "remote_id": res["id"],
                         "source": {"facility_oid": src_fac,
                                    "facility": (facilities.get_by_oid(src_fac) or {}).get("name", src_fac),
                                    "held": "remote", "remote": True, "via": f["oid"]}})
            vals.setdefault("source_facility", src_fac)
            vals.pop("content", None)
            items.setdefault(rtype, []).append(vals)
    return {"items": items, "imaging": imaging, "errors": errors, "facilities": fac_oids,
            "matches": disc["matches"]}


def install() -> None:
    from ehr import chart
    chart.register_remote_source(remote_chart)
