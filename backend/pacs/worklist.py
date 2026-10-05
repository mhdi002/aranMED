"""Modality Worklist (MWL) and Modality Performed Procedure Step (MPPS).

This is the IHE Radiology Scheduled Workflow loop:

    order (HL7 ORM / FHIR ServiceRequest / UI)
      -> worklist entry, StudyInstanceUID pre-assigned
      -> modality pulls it with MWL C-FIND (no re-typing of demographics)
      -> MPPS N-CREATE "IN PROGRESS", images C-STOREd under that study UID
      -> MPPS N-SET "COMPLETED" (or "DISCONTINUED")

Status moves ``scheduled -> in_progress -> completed`` (or ``discontinued``
/ ``cancelled``). Images arriving with a matching AccessionNumber move a
scheduled entry to in_progress even if the modality never sends MPPS.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import db
from clinicaldb import mpi, settings
from clinicaldb.util import jdump, jload, new_id, norm_date, row
from pacs import schema  # noqa: F401

log = logging.getLogger("pacs.worklist")

STATUSES = ("scheduled", "in_progress", "completed", "discontinued", "cancelled")

FIELDS = ("accession", "person_id", "patient_id", "issuer", "patient_name",
          "patient_birth_date", "patient_sex", "modality", "station_ae", "station_name",
          "scheduled_start", "procedure_code", "procedure_description",
          "requested_procedure_id", "sps_id", "referring_physician", "reason", "priority",
          "status", "study_uid", "order_id", "source")


def _uid() -> str:
    from pydicom.uid import generate_uid
    root = settings.env("DICOM_UID_ROOT", "")
    return str(generate_uid(prefix=(root.rstrip(".") + ".") if root else None))


def dicom_dt(value: Any) -> Optional[str]:
    """Canonical DICOM date-time ``YYYYMMDD[HHMM[SS]]`` from ISO, HL7 or DICOM
    input (``2026-10-02T09:00:00Z``, ``202610020900``, ``20261002T090000``)."""
    if value in (None, ""):
        return None
    import re
    s = str(value).strip()
    s = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", s)     # zone
    s = s.split(".")[0]                                # fraction
    digits = re.sub(r"\D", "", s)[:14]
    return digits or None


def generate_uid() -> str:
    """A new DICOM UID under ``DICOM_UID_ROOT`` (or pydicom's root)."""
    return _uid()


def create(data: dict[str, Any]) -> dict:
    d = {k: data.get(k) for k in FIELDS if data.get(k) is not None}
    if not d.get("accession"):
        d["accession"] = "ACC" + new_id()[:12].upper()
    if not d.get("modality"):
        raise ValueError("modality is required")
    d.setdefault("status", "scheduled")
    if d["status"] not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    d.setdefault("study_uid", _uid())
    d.setdefault("sps_id", "SPS" + new_id()[:8].upper())
    d.setdefault("requested_procedure_id", "RP" + new_id()[:8].upper())
    if d.get("patient_birth_date"):
        d["patient_birth_date"] = (norm_date(d["patient_birth_date"]) or "").replace("-", "")
    if d.get("scheduled_start"):
        d["scheduled_start"] = dicom_dt(d["scheduled_start"])
    # Resolve / register the patient so the worklist is tied to the MPI.
    if not d.get("person_id") and (d.get("patient_id") or d.get("patient_name")):
        idents = []
        if d.get("patient_id"):
            issuer = d.get("issuer") or settings.facility_oid()
            idents.append({"system": f"urn:oid:{issuer}" if issuer[0].isdigit() else issuer,
                           "value": d["patient_id"], "type": "MR", "facility_oid": issuer})
        d["person_id"] = mpi.register_person(
            {"name": d.get("patient_name"), "birth_date": d.get("patient_birth_date"),
             "sex": d.get("patient_sex")}, idents)["person_id"]
    now = db.now()
    wid = new_id()
    cols = list(d) + ["id", "created_at", "updated_at"]
    with db.connect() as c:
        c.execute(f"INSERT INTO pacs_worklist({', '.join(cols)}) VALUES "
                  f"({', '.join('?' for _ in cols)})", (*d.values(), wid, now, now))
    return get(wid)  # type: ignore[return-value]


def get(wid: str) -> Optional[dict]:
    with db.connect() as c:
        return row(c.execute("SELECT * FROM pacs_worklist WHERE id=? OR accession=?",
                             (wid, wid)).fetchone())


def update(wid: str, changes: dict[str, Any]) -> Optional[dict]:
    cur = get(wid)
    if not cur:
        return None
    vals = {k: v for k, v in changes.items() if k in FIELDS and k != "accession"}
    if vals.get("scheduled_start"):
        vals["scheduled_start"] = dicom_dt(vals["scheduled_start"])
    if "status" in vals and vals["status"] not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if not vals:
        return cur
    with db.connect() as c:
        c.execute(f"UPDATE pacs_worklist SET {', '.join(f'{k}=?' for k in vals)}, updated_at=? "
                  "WHERE id=?", (*vals.values(), db.now(), cur["id"]))
    return get(cur["id"])


def list_entries(*, status: Optional[str] = None, modality: Optional[str] = None,
                 station_ae: Optional[str] = None, date: Optional[str] = None,
                 person_id: Optional[str] = None, limit: int = 200) -> list[dict]:
    clauses, params = [], []
    for col, val in (("status", status), ("modality", modality), ("station_ae", station_ae),
                     ("person_id", person_id)):
        if val:
            clauses.append(f"{col}=?")
            params.append(val)
    if date:
        clauses.append("scheduled_start LIKE ?")
        params.append((dicom_dt(date) or "")[:8] + "%")
    sql = "SELECT * FROM pacs_worklist"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY scheduled_start, created_at LIMIT ?"
    with db.connect() as c:
        return [row(r) for r in c.execute(sql, (*params, limit)).fetchall()]


# ---------------------------------------------------------------------------
# MWL C-FIND
# ---------------------------------------------------------------------------
def mwl_query(identifier, *, charset: Optional[str] = None) -> list:
    """Answer a Modality Worklist C-FIND identifier with matching datasets.

    Responses are UTF-8 (ISO_IR 192) unless the querying node is configured
    with another character set (older modalities: e.g. ``ISO_IR 100``)."""
    from pydicom.dataset import Dataset

    def val(ds, kw):
        v = ds.get(kw) if ds is not None else None
        return str(v).strip() if v not in (None, "") else ""

    sps = None
    if "ScheduledProcedureStepSequence" in identifier and identifier.ScheduledProcedureStepSequence:
        sps = identifier.ScheduledProcedureStepSequence[0]
    clauses, params = ["status IN ('scheduled','in_progress')"], []
    modality = val(sps, "Modality")
    station = val(sps, "ScheduledStationAETitle")
    start = val(sps, "ScheduledProcedureStepStartDate")
    if modality and modality != "*":
        clauses.append("modality=?")
        params.append(modality)
    if station and station != "*":
        clauses.append("station_ae=?")
        params.append(station)
    if start and start != "*":
        if "-" in start:
            lo, hi = start.split("-", 1)
            if lo:
                clauses.append("scheduled_start >= ?")
                params.append(lo)
            if hi:
                clauses.append("scheduled_start <= ?")
                params.append(hi + "~")
        else:
            clauses.append("scheduled_start LIKE ?")
            params.append(start + "%")
    for kw, col in (("PatientID", "patient_id"), ("AccessionNumber", "accession")):
        v = val(identifier, kw)
        if v and v != "*":
            if "*" in v or "?" in v:
                clauses.append(f"{col} LIKE ?")
                params.append(v.replace("*", "%").replace("?", "_"))
            else:
                clauses.append(f"{col}=?")
                params.append(v)
    pn = val(identifier, "PatientName")
    if pn and pn != "*":
        clauses.append("LOWER(patient_name) LIKE ?")
        params.append(pn.replace("*", "%").replace("?", "_").lower())
    with db.connect() as c:
        rows = c.execute("SELECT * FROM pacs_worklist WHERE " + " AND ".join(clauses)
                         + " ORDER BY scheduled_start", params).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        ds = Dataset()
        ds.SpecificCharacterSet = charset or "ISO_IR 192"
        ds.PatientName = r.get("patient_name") or ""
        ds.PatientID = r.get("patient_id") or ""
        ds.IssuerOfPatientID = r.get("issuer") or ""
        ds.PatientBirthDate = r.get("patient_birth_date") or ""
        ds.PatientSex = r.get("patient_sex") or ""
        ds.AccessionNumber = r["accession"]
        ds.ReferringPhysicianName = r.get("referring_physician") or ""
        ds.StudyInstanceUID = r["study_uid"]
        ds.RequestedProcedureID = r.get("requested_procedure_id") or ""
        ds.RequestedProcedureDescription = r.get("procedure_description") or ""
        ds.RequestedProcedurePriority = r.get("priority") or ""
        ds.ReasonForTheRequestedProcedure = r.get("reason") or ""
        item = Dataset()
        item.Modality = r.get("modality") or ""
        item.ScheduledStationAETitle = r.get("station_ae") or ""
        item.ScheduledStationName = r.get("station_name") or ""
        ss = (r.get("scheduled_start") or "").replace("-", "").replace(":", "").replace("T", "")
        item.ScheduledProcedureStepStartDate = ss[:8]
        item.ScheduledProcedureStepStartTime = ss[8:14]
        item.ScheduledProcedureStepID = r.get("sps_id") or ""
        item.ScheduledProcedureStepDescription = r.get("procedure_description") or ""
        item.ScheduledProcedureStepStatus = "SCHEDULED" if r["status"] == "scheduled" else "STARTED"
        if r.get("procedure_code"):
            code = Dataset()
            code.CodeValue = r["procedure_code"]
            code.CodingSchemeDesignator = settings.env("PROCEDURE_CODING_SCHEME", "LOCAL")
            code.CodeMeaning = r.get("procedure_description") or r["procedure_code"]
            item.ScheduledProtocolCodeSequence = [code]
        ds.ScheduledProcedureStepSequence = [item]
        out.append(ds)
    return out


# ---------------------------------------------------------------------------
# MPPS
# ---------------------------------------------------------------------------
_MPPS_STATUS = {"IN PROGRESS": "in_progress", "COMPLETED": "completed",
                "DISCONTINUED": "discontinued"}


def _ref_accession(ds) -> tuple[Optional[str], Optional[str]]:
    acc, study = None, None
    seq = ds.get("ScheduledStepAttributesSequence") if ds is not None else None
    if seq:
        acc = str(seq[0].get("AccessionNumber") or "") or None
        study = str(seq[0].get("StudyInstanceUID") or "") or None
    return acc, study


def mpps_get(sop_uid: str) -> Optional[dict]:
    with db.connect() as c:
        return row(c.execute("SELECT * FROM pacs_mpps WHERE sop_uid=?", (sop_uid,)).fetchone())


def mpps_create(sop_uid: str, ds, station_ae: Optional[str] = None) -> dict:
    status = str(ds.get("PerformedProcedureStepStatus") or "IN PROGRESS").upper()
    acc, study = _ref_accession(ds)
    wl = get(acc) if acc else None
    now = db.now()
    with db.connect() as c:
        c.execute("INSERT INTO pacs_mpps(sop_uid, worklist_id, accession, study_uid, status, "
                  "modality, station_ae, started_at, data, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (sop_uid, wl["id"] if wl else None, acc, study or (wl or {}).get("study_uid"),
                   status, str(ds.get("Modality") or "") or None, station_ae,
                   f"{ds.get('PerformedProcedureStepStartDate', '')}"
                   f"{ds.get('PerformedProcedureStepStartTime', '')}" or None,
                   jdump({"description": str(ds.get("PerformedProcedureStepDescription") or "")}),
                   now, now))
    if wl and status in _MPPS_STATUS:
        update(wl["id"], {"status": _MPPS_STATUS[status]})
    return {"sop_uid": sop_uid, "status": status, "worklist_id": wl["id"] if wl else None}


def mpps_set(sop_uid: str, mod) -> Optional[dict]:
    with db.connect() as c:
        r = row(c.execute("SELECT * FROM pacs_mpps WHERE sop_uid=?", (sop_uid,)).fetchone())
    if not r:
        return None
    status = str(mod.get("PerformedProcedureStepStatus") or r["status"]).upper()
    if r["status"] in ("COMPLETED", "DISCONTINUED"):
        raise ValueError("MPPS is already final")
    ended = f"{mod.get('PerformedProcedureStepEndDate', '')}{mod.get('PerformedProcedureStepEndTime', '')}"
    data = jload(r.get("data"), {})
    series = []
    for perf in mod.get("PerformedSeriesSequence") or []:
        series.append({"series_uid": str(perf.get("SeriesInstanceUID") or ""),
                       "instances": [str(x.get("ReferencedSOPInstanceUID") or "")
                                     for x in perf.get("ReferencedImageSequence") or []]})
    if series:
        data["performed_series"] = series
    with db.connect() as c:
        c.execute("UPDATE pacs_mpps SET status=?, ended_at=?, data=?, updated_at=? WHERE sop_uid=?",
                  (status, ended or None, jdump(data), db.now(), sop_uid))
    if r.get("worklist_id") and status in _MPPS_STATUS:
        update(r["worklist_id"], {"status": _MPPS_STATUS[status]})
    return {"sop_uid": sop_uid, "status": status}


def list_mpps(limit: int = 100) -> list[dict]:
    with db.connect() as c:
        return [row(r) for r in c.execute(
            "SELECT * FROM pacs_mpps ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]


# Images arriving under a worklist accession advance the entry.
def _on_ingest(study: dict, new_study: bool) -> None:
    acc = study.get("accession")
    if not acc:
        return
    wl = get(acc)
    if not wl:
        return
    changes: dict[str, Any] = {}
    if wl["status"] == "scheduled":
        changes["status"] = "in_progress"
    if wl.get("study_uid") != study["study_uid"]:
        changes["study_uid"] = study["study_uid"]
    if changes:
        update(wl["id"], changes)
    if new_study and wl.get("priority"):
        with db.connect() as c:
            c.execute("UPDATE pacs_studies SET priority=? WHERE study_uid=?",
                      (wl["priority"], study["study_uid"]))


from pacs import index as _index  # noqa: E402

_index.register_ingest_hook(_on_ingest)
