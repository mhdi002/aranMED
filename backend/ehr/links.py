"""Glue between the EHR and the PACS so the two databases stay one record.

* A study report saved in the PACS (draft → final) is mirrored as an EHR
  ``diagnostic_report`` (category RAD) carrying the StudyInstanceUID.
* An active imaging ``service_request`` (order) with no accession yet is
  placed on the modality worklist; the accession is written back to the
  order, so MWL → MPPS → images → report all trace to the order.
* Observations with a reference range and no interpretation get one
  (L/N/H), so abnormal results surface in the chart summary.
"""
from __future__ import annotations

import logging

from clinicaldb import settings
from ehr import store

log = logging.getLogger("ehr.links")

_STATUS = {"draft": "registered", "preliminary": "preliminary", "final": "final",
           "amended": "amended", "cancelled": "cancelled"}


def _on_report(rep: dict) -> None:
    if not rep.get("person_id"):
        return
    from pacs import index
    study = index.get_study(rep["study_uid"]) or {}
    impression = rep.get("text") or ""
    for marker in ("IMPRESSION:", "Impression:", "CONCLUSION:"):
        if marker in impression:
            impression = impression.split(marker, 1)[1].strip()
            break
    d = study.get("StudyDate") or ""
    store.create("diagnostic_report", {
        "person_id": rep["person_id"], "source_id": f"pacs-report:{rep['id']}",
        "category": "RAD", "status": _STATUS.get(rep["status"], "registered"),
        "display": study.get("StudyDescription") or "Imaging report",
        "text": rep.get("text"), "conclusion": impression[:2000],
        "effective": f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else None,
        "study_uid": rep["study_uid"], "performer": rep.get("author"),
        "data": {"modalities": study.get("ModalitiesInStudy"),
                 "accession": study.get("AccessionNumber"), "source": rep.get("source")},
    }, actor=rep.get("author"))


def _from_peer_hospital(item: dict) -> bool:
    """Orders authored at another hospital (arriving in a transfer package)
    belong to that hospital's worklist, not ours — they stay as history."""
    src = item.get("source_facility")
    if not src or src == settings.facility_oid():
        return False
    from clinicaldb import facilities
    f = facilities.get_by_oid(src)
    return bool(f and not f.get("is_local") and (f.get("kind") or "hospital") != "his")


def _on_resource(rtype: str, item: dict, action: str) -> None:
    if rtype == "service_request" and action == "create":
        from pacs import worklist
        if (item.get("category") or "").lower() == "imaging" and item.get("status") == "active" \
                and not (item.get("accession") and worklist.get(item["accession"])) \
                and not _from_peer_hospital(item):
            from clinicaldb import mpi
            person = mpi.get(item["person_id"]) or {}
            demo = person.get("demographics") or {}
            mrn = mpi.local_mrn(item["person_id"]) or mpi.ensure_local_mrn(item["person_id"])
            data = item.get("data") or {}
            try:
                wl = worklist.create({
                    "person_id": item["person_id"], "patient_id": mrn,
                    "issuer": settings.facility_oid(),
                    "patient_name": "^".join(x for x in (demo.get("family"), demo.get("given")) if x),
                    "patient_birth_date": demo.get("birth_date"),
                    "patient_sex": {"male": "M", "female": "F"}.get(demo.get("sex") or "", "O"),
                    "modality": data.get("modality") or "OT",
                    "procedure_code": item.get("code"),
                    "procedure_description": item.get("display") or item.get("text"),
                    "priority": (item.get("priority") or "").upper() or None,
                    "reason": item.get("reason"), "referring_physician": item.get("requester"),
                    "scheduled_start": item.get("occurrence"), "station_ae": data.get("station_ae"),
                    "order_id": item["id"], "source": "ehr",
                    # An accession assigned upstream (HL7 filler number) is kept.
                    **({"accession": item["accession"]} if item.get("accession") else {})})
                if wl["accession"] != item.get("accession"):
                    store.update("service_request", item["id"], {"accession": wl["accession"]})
            except Exception:  # noqa: BLE001
                log.exception("could not place order %s on the worklist", item["id"])
    if rtype == "observation" and action == "create" and not item.get("interpretation"):
        v, lo, hi = item.get("value_num"), item.get("ref_low"), item.get("ref_high")
        if v is not None and (lo is not None or hi is not None):
            interp = "L" if lo is not None and v < lo else "H" if hi is not None and v > hi else "N"
            import db
            with db.connect() as c:
                c.execute("UPDATE ehr_observations SET interpretation=? WHERE id=?",
                          (interp, item["id"]))


def install() -> None:
    from pacs import reports
    reports.register_hook(_on_report)
    store.register_hook(_on_resource)
