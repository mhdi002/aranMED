"""EMS → hospital: pre-arrival notification and handoff.

Accepts a patient care report from an ambulance service in any of:

* **NEMSIS v3.5 XML** (``EMSDataSet`` / ``PatientCareReport``) — the
  elements used are the national ones: ePatient.02/.03/.13/.17/.12,
  eResponse.03/.13/.14, eTimes, eSituation.04/.11/.13, eVitals (BP, HR, RR,
  SpO2, GCS, temperature, glucose, time), eMedications.03, eProcedures.03,
  eNarrative.01, eDisposition destination ETA.
* **NEMSIS-shaped JSON** — the same element names as keys.
* **FHIR Bundle** — Patient + Encounter + Observations (+ Procedure,
  MedicationAdministration-as-statement) from an EMS FHIR system.

Each report becomes: an MPI person (identified by name/DOB/national id, or a
provisional identity keyed by the incident number when unknown), a planned
EMER Encounter, vital-sign Observations (LOINC), given medications and
procedures, the raw ePCR as a DocumentReference, and an ``ems_notifications``
row on the ED board. ED staff are alerted through the existing alert
channels (``EMS_ALERT_EMAIL`` / ``EMS_ALERT_SMS``; dry-run when unset).
Re-sending the same incident updates it in place.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from lxml import etree

import db
from clinicaldb import messages, mpi, settings
from clinicaldb.util import jdump, jload, new_id, row
from ehr import store
from interop import fhir_map

log = logging.getLogger("interop.ems")

VITAL_MAP = {  # key: (NEMSIS element, LOINC, display, unit)
    "sbp": ("eVitals.06", "8480-6", "Systolic blood pressure", "mm[Hg]"),
    "dbp": ("eVitals.07", "8462-4", "Diastolic blood pressure", "mm[Hg]"),
    "hr": ("eVitals.10", "8867-4", "Heart rate", "/min"),
    "spo2": ("eVitals.12", "59408-5", "Oxygen saturation", "%"),
    "rr": ("eVitals.14", "9279-1", "Respiratory rate", "/min"),
    "glucose": ("eVitals.18", "2339-0", "Glucose", "mg/dL"),
    "gcs": ("eVitals.23", "9269-2", "Glasgow coma score total", "{score}"),
    "temp": ("eVitals.24", "8310-5", "Body temperature", "Cel"),
    "pain": ("eVitals.27", "72514-3", "Pain severity", "{score}"),
}
_SEX = {"9906001": "female", "9906003": "male", "F": "female", "M": "male",
        "female": "female", "male": "male"}
_ACUITY = {"2813001": "critical", "2813003": "emergent", "2813005": "lower acuity",
           "2813007": "dead without resuscitation"}


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def parse_nemsis_xml(xml: bytes) -> dict[str, Any]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(xml, parser)

    def all_(name: str) -> list[str]:
        return [e.text.strip() for e in root.iter() if isinstance(e.tag, str)
                and etree.QName(e).localname == name and e.text and e.text.strip()]

    def one(name: str) -> Optional[str]:
        vals = all_(name)
        return vals[0] if vals else None
    vitals_groups = []
    for grp in root.iter():
        if isinstance(grp.tag, str) and etree.QName(grp).localname == "eVitals.VitalGroup":
            g: dict[str, Any] = {}
            for e in grp.iter():
                if isinstance(e.tag, str) and e.text and e.text.strip():
                    g[etree.QName(e).localname] = e.text.strip()
            vitals_groups.append(g)
    flat = {k: one(k) for k in ("ePatient.02", "ePatient.03", "ePatient.12", "ePatient.13",
                                "ePatient.17", "eResponse.01", "eResponse.03", "eResponse.13",
                                "eResponse.14", "eSituation.04", "eSituation.11", "eSituation.13",
                                "eNarrative.01", "eTimes.07", "eTimes.11", "eDisposition.01",
                                "eDisposition.02")}
    flat["eMedications.03"] = all_("eMedications.03")
    flat["eProcedures.03"] = all_("eProcedures.03")
    flat["vitals"] = vitals_groups or [{k: one(v[0]) for k, v in VITAL_MAP.items()}]
    return flat


def normalise(payload: dict[str, Any]) -> dict[str, Any]:
    """NEMSIS-keyed dict -> internal shape."""
    vitals = []
    for g in payload.get("vitals") or []:
        v = {}
        for key, (el, *_rest) in VITAL_MAP.items():
            val = g.get(el) if el in g else g.get(key)
            if val not in (None, ""):
                try:
                    v[key] = float(val)
                except (TypeError, ValueError):
                    pass
        if v:
            v["time"] = g.get("eVitals.01") or g.get("time")
            vitals.append(v)
    return {
        "agency": payload.get("eResponse.01") or payload.get("agency"),
        "incident": payload.get("eResponse.03") or payload.get("incident"),
        "unit": payload.get("eResponse.14") or payload.get("eResponse.13") or payload.get("unit"),
        "patient": {"family": payload.get("ePatient.02"), "given": payload.get("ePatient.03"),
                    "sex": _SEX.get(str(payload.get("ePatient.13") or ""), None),
                    "birth_date": payload.get("ePatient.17"),
                    "national_id": payload.get("ePatient.12")},
        "chief_complaint": payload.get("eSituation.04") or payload.get("chief_complaint"),
        "impression": payload.get("eSituation.11") or payload.get("impression"),
        "acuity": _ACUITY.get(str(payload.get("eSituation.13") or ""), payload.get("eSituation.13") or payload.get("acuity")),
        "narrative": payload.get("eNarrative.01") or payload.get("narrative"),
        "eta": payload.get("eTimes.11") or payload.get("eta"),
        "medications": [m for m in (payload.get("eMedications.03") or []) if m],
        "procedures": [p for p in (payload.get("eProcedures.03") or []) if p],
        "vitals": vitals,
    }


def from_fhir_bundle(bundle: dict) -> dict[str, Any]:
    pat, enc, obs, procs, meds = None, None, [], [], []
    for e in bundle.get("entry") or []:
        r = e.get("resource") or {}
        rt = r.get("resourceType")
        if rt == "Patient":
            pat = r
        elif rt == "Encounter":
            enc = r
        elif rt == "Observation":
            obs.append(r)
        elif rt == "Procedure":
            procs.append(((r.get("code") or {}).get("text")) or "procedure")
        elif rt in ("MedicationAdministration", "MedicationStatement"):
            meds.append(((r.get("medicationCodeableConcept") or {}).get("text")) or "medication")
    demo, idents = fhir_map.patient_in(pat) if pat else ({}, [])
    v: dict[str, Any] = {}
    by_code = {c[1]: k for k, c in VITAL_MAP.items()}
    for o in obs:
        code = ((o.get("code") or {}).get("coding") or [{}])[0].get("code")
        if code == "85354-9":
            for comp in o.get("component") or []:
                cc = ((comp.get("code") or {}).get("coding") or [{}])[0].get("code")
                if cc in by_code:
                    v[by_code[cc]] = (comp.get("valueQuantity") or {}).get("value")
        elif code in by_code:
            v[by_code[code]] = (o.get("valueQuantity") or {}).get("value")
    nat = next((i["value"] for i in idents if i["system"] == settings.national_id_system()), None)
    return {"incident": (enc or {}).get("id") or bundle.get("id"),
            "unit": (((enc or {}).get("serviceProvider")) or {}).get("display"),
            "patient": {**demo, "national_id": nat},
            "chief_complaint": (((enc or {}).get("reasonCode") or [{}])[0]).get("text"),
            "acuity": ((enc or {}).get("priority") or {}).get("text"),
            "eta": ((enc or {}).get("period") or {}).get("end"),
            "medications": meds, "procedures": procs, "vitals": [v] if v else [],
            "narrative": None, "identifiers": idents}


def ingest(raw: bytes | str | dict, *, content_type: str = "application/json",
           source_facility: Optional[str] = None, actor: Optional[str] = None) -> dict[str, Any]:
    if isinstance(raw, dict):
        data = raw
        payload = from_fhir_bundle(data) if data.get("resourceType") == "Bundle" else normalise(data)
        raw_text = json.dumps(data, ensure_ascii=False)
    else:
        raw_text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        if "xml" in content_type or raw_text.lstrip().startswith("<"):
            payload = normalise(parse_nemsis_xml(raw_text.encode("utf-8")))
            content_type = "application/xml"
        else:
            data = json.loads(raw_text)
            payload = from_fhir_bundle(data) if data.get("resourceType") == "Bundle" else normalise(data)
    src = source_facility or settings.env("EMS_DEFAULT_AGENCY_OID", "") or settings.facility_oid()
    incident = payload.get("incident") or new_id()[:12]
    p = payload["patient"]
    idents = list(payload.get("identifiers") or [])
    if p.get("national_id") and not any(i["system"] == settings.national_id_system() for i in idents):
        idents.append({"system": settings.national_id_system(), "value": p["national_id"], "type": "NI"})
    idents.append({"system": f"urn:aranmed:ems-incident:{src}", "value": str(incident), "type": "VN"})
    demo = {k: v for k, v in p.items() if k != "national_id" and v}
    if not (demo.get("family") or demo.get("given")):
        demo = {"family": "Unknown", "given": f"EMS {incident}", "sex": p.get("sex")}
    reg = mpi.register_person(demo, idents, source_facility=src)
    pid = reg["person_id"]
    mrn = mpi.ensure_local_mrn(pid)
    sid = f"ems:{incident}"
    enc = store.create("encounter", {
        "person_id": pid, "source_facility": src, "source_id": sid, "class": "EMER",
        "status": "planned", "reason": payload.get("chief_complaint"),
        "type_text": "EMS pre-arrival", "priority": payload.get("acuity"),
        "admit_source": "EMS", "start_at": payload.get("eta"),
        "text": payload.get("impression")}, actor=actor)
    for i, v in enumerate(payload["vitals"]):
        when = v.get("time")
        for key, (_el, loinc, disp, unit) in VITAL_MAP.items():
            if v.get(key) is None:
                continue
            store.create("observation", {
                "person_id": pid, "source_facility": src, "source_id": f"{sid}:vital:{i}:{key}",
                "encounter_id": enc["id"], "category": "vital-signs",
                "code_system": "http://loinc.org", "code": loinc, "display": disp,
                "value_num": v[key], "unit": unit, "effective": when, "status": "final",
                "performer": payload.get("unit")}, actor=actor)
    for i, m in enumerate(payload["medications"]):
        store.create("medication", {"person_id": pid, "source_facility": src,
                                    "source_id": f"{sid}:med:{i}", "kind": "statement",
                                    "status": "completed", "display": m, "text": m,
                                    "note": "Given by EMS", "encounter_id": enc["id"]}, actor=actor)
    for i, pr_ in enumerate(payload["procedures"]):
        store.create("procedure", {"person_id": pid, "source_facility": src,
                                   "source_id": f"{sid}:proc:{i}", "display": pr_, "text": pr_,
                                   "status": "completed", "performer": payload.get("unit"),
                                   "encounter_id": enc["id"]}, actor=actor)
    store.create("document", {"person_id": pid, "source_facility": src, "source_id": f"{sid}:epcr",
                              "doc_type": "ems-report", "title": f"EMS ePCR {incident}",
                              "content_type": content_type if "xml" in content_type else "application/json",
                              "content": raw_text, "encounter_id": enc["id"], "status": "current"},
                 actor=actor)
    latest = payload["vitals"][-1] if payload["vitals"] else {}
    summary = handoff_summary(payload)
    now = db.now()
    with db.connect() as c:
        ex = c.execute("SELECT id FROM ems_notifications WHERE source_facility=? AND source_id=?",
                       (src, str(incident))).fetchone()
        if ex:
            nid = ex["id"]
            c.execute("UPDATE ems_notifications SET person_id=?, encounter_id=?, unit=?, eta=?, triage=?, "
                      "chief_complaint=?, summary=?, data=?, updated_at=? WHERE id=?",
                      (pid, enc["id"], payload.get("unit"), payload.get("eta"), payload.get("acuity"),
                       payload.get("chief_complaint"), summary, jdump({**payload, "latest_vitals": latest}),
                       now, nid))
        else:
            nid = new_id()
            c.execute("INSERT INTO ems_notifications(id, person_id, encounter_id, source_facility, "
                      "source_id, unit, eta, status, triage, chief_complaint, summary, data, "
                      "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (nid, pid, enc["id"], src, str(incident), payload.get("unit"), payload.get("eta"),
                       "inbound", payload.get("acuity"), payload.get("chief_complaint"), summary,
                       jdump({**payload, "latest_vitals": latest}), now, now))
    alerts = _alert_ed(payload, summary, mrn)
    messages.log(direction="in", protocol="ems", message_type="ePCR", peer=src, status="ok",
                 person_id=pid, control_id=str(incident), payload=raw_text[:20000])
    return {"notification_id": nid, "person_id": pid, "encounter_id": enc["id"], "mrn": mrn,
            "mpi_outcome": reg["outcome"], "alerts": alerts}


def handoff_summary(p: dict) -> str:
    """MIST handoff: Mechanism/Medical complaint, Injuries/Impression, Signs, Treatment."""
    v = p["vitals"][-1] if p.get("vitals") else {}
    signs = ", ".join(f"{k.upper()} {v[k]:g}" for k in ("sbp", "dbp", "hr", "rr", "spo2", "gcs", "temp", "glucose")
                      if v.get(k) is not None)
    if v.get("sbp") is not None and v.get("dbp") is not None:
        signs = f"BP {v['sbp']:g}/{v['dbp']:g}, " + ", ".join(
            f"{k.upper()} {v[k]:g}" for k in ("hr", "rr", "spo2", "gcs", "temp", "glucose") if v.get(k) is not None)
    treat = "; ".join(p.get("medications", []) + p.get("procedures", [])) or "none recorded"
    return (f"M: {p.get('chief_complaint') or 'unknown'}\n"
            f"I: {p.get('impression') or 'n/a'} (acuity: {p.get('acuity') or 'n/a'})\n"
            f"S: {signs or 'no vitals'}\n"
            f"T: {treat}\n"
            f"Unit {p.get('unit') or '?'} · ETA {p.get('eta') or '?'}")


def _alert_ed(payload: dict, summary: str, mrn: str) -> list[dict]:
    from tools import alerts
    out = []
    body = f"EMS INBOUND ({payload.get('acuity') or 'acuity n/a'}) MRN {mrn}\n{summary}"
    email = settings.env("EMS_ALERT_EMAIL", "")
    sms = settings.env("EMS_ALERT_SMS", "")
    if email:
        out.append(alerts.send_email(to=email, subject="EMS pre-arrival notification", body=body))
    if sms:
        out.append(alerts.send_sms(to=sms, body=body[:300]))
    return out


# ---------------------------------------------------------------- board
def board(status: Optional[str] = None, limit: int = 100) -> list[dict]:
    sql, params = "SELECT * FROM ems_notifications", []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    with db.connect() as c:
        rows = c.execute(sql + " ORDER BY updated_at DESC LIMIT ?", (*params, limit)).fetchall()
    out = []
    for r in rows:
        d = row(r)
        d["data"] = jload(d.get("data"), {})
        p = mpi.get(d["person_id"]) if d.get("person_id") else None
        d["patient"] = {"name": p["name"], "demographics": p["demographics"],
                        "mrn": mpi.local_mrn(p["id"])} if p else None
        out.append(d)
    return out


def get(nid: str) -> Optional[dict]:
    with db.connect() as c:
        r = c.execute("SELECT id FROM ems_notifications WHERE id=?", (nid,)).fetchone()
    if not r:
        return None
    return next((x for x in board(limit=10000) if x["id"] == nid), None)


_ENC_STATUS = {"acknowledged": "planned", "arrived": "arrived", "handed_over": "in-progress",
               "cancelled": "cancelled"}


def set_status(nid: str, status: str, user: dict) -> dict:
    if status not in _ENC_STATUS:
        raise ValueError(f"status must be one of {list(_ENC_STATUS)}")
    n = get(nid)
    if not n:
        raise KeyError(nid)
    with db.connect() as c:
        c.execute("UPDATE ems_notifications SET status=?, acknowledged_by=COALESCE(acknowledged_by, ?), "
                  "updated_at=? WHERE id=?", (status, user.get("username"), db.now(), nid))
    if n.get("encounter_id"):
        store.update("encounter", n["encounter_id"], {"status": _ENC_STATUS[status]},
                     actor=user.get("username"))
    return get(nid)
