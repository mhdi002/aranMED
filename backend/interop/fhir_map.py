"""Two-way mapping between the relational EHR / MPI / PACS and FHIR R4.

``to_fhir(rtype, row)`` produces a FHIR R4 resource from an internal row;
``from_fhir(resource)`` returns ``(rtype, values)`` ready for
``ehr.store.create`` — used by the FHIR server's create/update/transaction,
by transfer-package import and by the live federated chart.

Provenance survives the round trip: ``meta.source`` is
``urn:oid:<authoring facility>#<id there>``, so a record that travels
A → B → C keeps its origin and a second import de-duplicates instead of
copying. Codes keep the original ``text`` beside any coding, as in
backend/fhir.py.
"""
from __future__ import annotations

import base64
import re
import time
from typing import Any, Optional

from clinicaldb import facilities, mpi, settings

V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
OBS_CAT = "http://terminology.hl7.org/CodeSystem/observation-category"
OBS_INTERP = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
COND_CLIN = "http://terminology.hl7.org/CodeSystem/condition-clinical"
COND_VER = "http://terminology.hl7.org/CodeSystem/condition-ver-status"
COND_CAT = "http://terminology.hl7.org/CodeSystem/condition-category"
ALLERGY_CLIN = "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical"
DIAG_SVC = "http://terminology.hl7.org/CodeSystem/v2-0074"
CONSENT_SCOPE = "http://terminology.hl7.org/CodeSystem/consentscope"
CONSENT_CAT = "urn:aranmed:consent-category"
UCUM = "http://unitsofmeasure.org"
LOINC = "http://loinc.org"
# AranMed extensions: carry columns FHIR has no exact slot for, so a record
# survives A -> B -> C without losing anything (other systems ignore them).
EXT_BASE = "https://aranmed.org/fhir/StructureDefinition/"
EXT_DATA = EXT_BASE + "row-data"
EXT_DOSE = EXT_BASE + "dose-text"
EXT_DOSE_HISTORY = EXT_BASE + "dose-history"
EXT_PANEL = EXT_BASE + "panel-id"
EXT_NOTE = EXT_BASE + "note"  # for resource types that have no Annotation slot
DCM = "http://dicom.nema.org/resources/ontology/DCM"

DOC_TYPES = {  # doc_type -> LOINC
    "discharge-summary": ("18842-5", "Discharge summary"),
    "progress-note": ("11506-3", "Progress note"),
    "referral-note": ("57133-1", "Referral note"),
    "transfer-summary": ("18761-7", "Transfer summary note"),
    "ems-report": ("67796-3", "EMS patient care report"),
    "cda": ("34133-9", "Summary of episode note"),
    "imaging-report": ("18748-4", "Diagnostic imaging study"),
    "consult-note": ("11488-4", "Consult note"),
}
_DOC_BY_LOINC = {v[0]: k for k, v in DOC_TYPES.items()}

TYPE_TO_FHIR = {
    "encounter": "Encounter", "condition": "Condition", "allergy": "AllergyIntolerance",
    "observation": "Observation", "procedure": "Procedure", "immunization": "Immunization",
    "document": "DocumentReference", "service_request": "ServiceRequest",
    "diagnostic_report": "DiagnosticReport", "consent": "Consent",
}
FHIR_TO_TYPE = {v: k for k, v in TYPE_TO_FHIR.items()}
FHIR_TO_TYPE.update({"MedicationStatement": "medication", "MedicationRequest": "medication"})


def fhir_type(rtype: str, row: Optional[dict] = None) -> str:
    if rtype == "medication":
        return "MedicationRequest" if (row or {}).get("kind") == "request" else "MedicationStatement"
    return TYPE_TO_FHIR[rtype]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def _fhir_datetime(v: Optional[str]) -> Optional[str]:
    """Normalise to a valid FHIR date/dateTime (adds a zone to full times)."""
    if not v:
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if re.fullmatch(r"\d{12,14}", s):
        s = f"{s[:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}:{s[12:14] or '00'}"
    if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", s):
        return s
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?", s)
    if m:
        return f"{m.group(1)}T{m.group(2)}{m.group(3) or ':00'}{m.group(5) or 'Z'}"
    return None


def _instant(v: Optional[str]) -> Optional[str]:
    """FHIR instant (full date-time with zone); a bare date becomes midnight UTC."""
    d = _fhir_datetime(v)
    if not d:
        return None
    return d if "T" in d else (f"{d}T00:00:00Z" if len(d) == 10 else None)


def _date_only(v: Optional[str]) -> Optional[str]:
    d = _fhir_datetime(v)
    return d[:10] if d else None


def cc(system: Optional[str], code: Optional[str], display: Optional[str],
       text: Optional[str] = None) -> Optional[dict]:
    out: dict[str, Any] = {}
    if code:
        c = {"code": code}
        if system:
            c["system"] = system
        if display:
            c["display"] = display
        out["coding"] = [c]
    t = text or display
    if t:
        out["text"] = t
    return out or None


def _cc_parts(c: Optional[dict]) -> dict[str, Optional[str]]:
    if not c:
        return {"code_system": None, "code": None, "display": None, "text": None}
    coding = (c.get("coding") or [{}])[0]
    return {"code_system": coding.get("system"), "code": coding.get("code"),
            "display": coding.get("display") or c.get("text"), "text": c.get("text")}


def _coding_code(c: Optional[dict]) -> Optional[str]:
    if not c:
        return None
    return ((c.get("coding") or [{}])[0]).get("code") or c.get("text")


def _ref_id(ref: Optional[dict], rtype: str) -> Optional[str]:
    r = (ref or {}).get("reference") or ""
    if r.startswith(f"{rtype}/"):
        return r.split("/", 1)[1].split("/")[0]
    return r or None


def _meta(row: dict) -> dict:
    src = row.get("source_facility") or settings.facility_oid()
    return {"versionId": str(row.get("version") or 1), "lastUpdated": _iso(row.get("updated_at")),
            "source": f"urn:oid:{src}#{row.get('source_id') or row.get('id')}"}


def _parse_source(res: dict) -> tuple[Optional[str], Optional[str]]:
    src = (res.get("meta") or {}).get("source") or ""
    m = re.match(r"urn:oid:([0-9.]+)#(.+)$", src)
    return (m.group(1), m.group(2)) if m else (None, None)


def _subject(row: dict) -> dict:
    return {"reference": f"Patient/{row['person_id']}"}


def _note(row: dict) -> Optional[list]:
    return [{"text": row["note"]}] if row.get("note") else None


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, [], {}, "")}


def _ext(url: str, value: Any) -> Optional[dict]:
    if value in (None, "", [], {}):
        return None
    if not isinstance(value, str):
        import json
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return {"url": url, "valueString": value}


def _ext_get(obj: Optional[dict], url: str, *, as_json: bool = False) -> Any:
    for e in (obj or {}).get("extension") or []:
        if e.get("url") == url:
            v = e.get("valueString")
            if as_json and v:
                import json
                try:
                    return json.loads(v)
                except ValueError:
                    return None
            return v
    return None


_NO_NOTE = {"Encounter", "DocumentReference", "DiagnosticReport", "Consent"}


def _with_common_ext(res: dict, row: dict) -> dict:
    exts = [e for e in (res.get("extension") or []) if e]
    d = _ext(EXT_DATA, row.get("data") or None)
    if d:
        exts.append(d)
    if res.get("resourceType") in _NO_NOTE and row.get("note"):
        exts.append(_ext(EXT_NOTE, row["note"]))
    res["extension"] = exts or None
    return res


_DOSE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z%µ/\[\]]+)?\s*$")


def _dose_quantity(dose: Optional[str]) -> Optional[dict]:
    m = _DOSE_RE.match(dose or "")
    if not m:
        return None
    q: dict[str, Any] = {"value": float(m.group(1))}
    if m.group(2):
        q.update({"unit": m.group(2), "system": UCUM, "code": m.group(2)})
    return q


def _strip_div(div: Optional[str]) -> Optional[str]:
    if not div:
        return None
    t = re.sub(r"<[^>]+>", "", div)
    return t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&") or None


# ---------------------------------------------------------------------------
# Patient / Organization / Endpoint / ImagingStudy
# ---------------------------------------------------------------------------
def patient(person: dict) -> dict:
    d = person.get("demographics") or {}
    idents = []
    for i in person.get("identifiers") or []:
        e = {"system": i["system"], "value": i["value"],
             "type": {"coding": [{"system": V2_0203, "code": i.get("type") or "MR"}]}}
        if i.get("facility_oid"):
            e["assigner"] = {"reference": f"Organization/{i['facility_oid']}"}
        idents.append(e)
    names = []
    if d.get("family") or d.get("given"):
        names.append(_clean({"use": "official", "family": d.get("family"),
                             "given": [g for g in (d.get("given") or "").split() if g] or None}))
    for a in d.get("aliases") or []:
        names.append(_clean({"use": "usual", "family": a.get("family"),
                             "given": [g for g in (a.get("given") or "").split() if g] or None}))
    addr = d.get("address")
    res = {
        "resourceType": "Patient", "id": person["id"],
        "meta": {"lastUpdated": _iso(person.get("updated_at")),
                 "source": f"urn:oid:{person.get('home_facility') or settings.facility_oid()}#{person['id']}"},
        "identifier": idents, "active": person.get("status", "active") == "active",
        "name": names, "gender": d.get("sex"),
        "birthDate": _date_only(d.get("birth_date")),
        "telecom": [{"system": "phone", "value": d["phone"]}] if d.get("phone") else None,
        "address": [addr if isinstance(addr, dict) else {"text": str(addr)}] if addr else None,
        "deceasedBoolean": d.get("deceased"),
        "communication": [{"language": {"coding": [{"system": "urn:ietf:bcp:47",
                                                     "code": d["language"]}]}}] if d.get("language") else None,
        "managingOrganization": {"reference": f"Organization/{person['home_facility']}"}
        if person.get("home_facility") else None,
    }
    if person.get("merged_into"):
        res["link"] = [{"other": {"reference": f"Patient/{person['merged_into']}"}, "type": "replaced-by"}]
    return _clean(res)


def patient_in(res: dict) -> tuple[dict, list[dict]]:
    """FHIR Patient -> (demographics, identifiers) for mpi.register_person."""
    name = next((n for n in res.get("name") or [] if n.get("use") in (None, "official")),
                (res.get("name") or [{}])[0] if res.get("name") else {})
    demo = {"family": name.get("family"), "given": " ".join(name.get("given") or []) or None,
            "sex": res.get("gender"), "birth_date": res.get("birthDate")}
    tel = next((t for t in res.get("telecom") or [] if t.get("system") == "phone"), None)
    if tel:
        demo["phone"] = tel.get("value")
    if res.get("address"):
        a = res["address"][0]
        demo["address"] = a.get("text") or ", ".join(a.get("line") or []) or None
    aliases = [{"family": n.get("family"), "given": " ".join(n.get("given") or []) or None}
               for n in res.get("name") or [] if n.get("use") == "usual"]
    if aliases:
        demo["aliases"] = aliases
    if res.get("deceasedBoolean") is not None:
        demo["deceased"] = res["deceasedBoolean"]
    lang = ((((res.get("communication") or [{}])[0]).get("language") or {}).get("coding") or [{}])[0].get("code")
    if lang:
        demo["language"] = lang
    idents = []
    for i in res.get("identifier") or []:
        if not (i.get("system") and i.get("value")):
            continue
        t = ((i.get("type") or {}).get("coding") or [{}])[0].get("code") or "MR"
        fac = None
        if i["system"].startswith("urn:oid:") and t == "MR":
            fac = i["system"][8:]
        idents.append({"system": i["system"], "value": i["value"], "type": t, "facility_oid": fac})
    return demo, idents


def organization(f: dict) -> dict:
    return _clean({
        "resourceType": "Organization", "id": f["oid"],
        "identifier": [{"system": "urn:ietf:rfc:3986", "value": f"urn:oid:{f['oid']}"}],
        "active": bool(f.get("active", True)), "name": f["name"],
        "type": [{"text": f.get("kind") or "hospital"}],
        "endpoint": [{"reference": f"Endpoint/dicomweb-{f['oid']}"}] if f.get("dicomweb_base") else None,
    })


def endpoint(f: dict) -> dict:
    return {"resourceType": "Endpoint", "id": f"dicomweb-{f['oid']}", "status": "active",
            "connectionType": {"system": "http://terminology.hl7.org/CodeSystem/endpoint-connection-type",
                               "code": "dicom-wado-rs"},
            "name": f"{f['name']} DICOMweb",
            "managingOrganization": {"reference": f"Organization/{f['oid']}"},
            "payloadType": [{"text": "DICOM"}],
            "address": f.get("dicomweb_base") or (settings.public_base_url() + settings.env(
                "DICOMWEB_PREFIX", "/api/dicom-web"))}


def imaging_study(study: dict, series: Optional[list[dict]] = None) -> dict:
    ext = study.get("_ext") or {}
    origin = ext.get("origin_facility") or settings.facility_oid()
    d, t = study.get("StudyDate") or "", (study.get("StudyTime") or "")[:6]
    started = None
    if len(d) == 8:
        started = f"{d[:4]}-{d[4:6]}-{d[6:]}" + (f"T{t[:2]}:{t[2:4]}:{t[4:6] or '00'}Z" if len(t) >= 4 else "")
    res = {
        "resourceType": "ImagingStudy", "id": study["StudyInstanceUID"],
        "meta": {"source": f"urn:oid:{origin}#{study['StudyInstanceUID']}"},
        "identifier": [{"system": "urn:dicom:uid", "value": f"urn:oid:{study['StudyInstanceUID']}"}]
        + ([{"type": {"coding": [{"system": V2_0203, "code": "ACSN"}]},
             "value": study["AccessionNumber"]}] if study.get("AccessionNumber") else []),
        "status": "available",
        "subject": {"reference": f"Patient/{ext['person_id']}"} if ext.get("person_id")
        else {"display": study.get("PatientName") or "unknown"},
        "started": started,
        "modality": [{"system": DCM, "code": m} for m in study.get("ModalitiesInStudy") or []],
        "numberOfSeries": study.get("NumberOfStudyRelatedSeries"),
        "numberOfInstances": study.get("NumberOfStudyRelatedInstances"),
        "description": study.get("StudyDescription"),
        "endpoint": [{"reference": f"Endpoint/dicomweb-{settings.facility_oid()}"}],
    }
    if series:
        res["series"] = [_clean({
            "uid": s["SeriesInstanceUID"], "number": s.get("SeriesNumber"),
            "modality": {"system": DCM, "code": s.get("Modality") or "OT"},
            "description": s.get("SeriesDescription"),
            "numberOfInstances": s.get("NumberOfSeriesRelatedInstances"),
            "bodySite": {"display": s["BodyPartExamined"]} if s.get("BodyPartExamined") else None,
        }) for s in series]
    return _clean(res)


# ---------------------------------------------------------------------------
# Clinical resources: internal -> FHIR
# ---------------------------------------------------------------------------
def to_fhir(rtype: str, row: dict) -> dict:
    fn = _TO.get(rtype)
    if fn is None:
        raise ValueError(f"no FHIR mapping for {rtype}")
    res = _with_common_ext(fn(row), row)
    res["id"] = row["id"]
    res["meta"] = _meta(row)
    return _clean(res)


def _enc(r: dict) -> dict:
    return {"resourceType": "Encounter", "status": r.get("status") or "finished",
            "class": {"system": ACT_CODE, "code": r.get("class") or "AMB"},
            "type": [{"text": r["type_text"]}] if r.get("type_text") else None,
            "subject": _subject(r),
            "period": _clean({"start": _fhir_datetime(r.get("start_at")),
                              "end": _fhir_datetime(r.get("end_at"))}) or None,
            "reasonCode": [{"text": r["reason"]}] if r.get("reason") else None,
            "location": [{"location": {"display": r["location"]}}] if r.get("location") else None,
            "serviceType": {"text": r["department"]} if r.get("department") else None,
            "hospitalization": _clean({
                "admitSource": {"text": r["admit_source"]} if r.get("admit_source") else None,
                "dischargeDisposition": {"text": r["disposition"]} if r.get("disposition") else None}) or None,
            "priority": {"text": r["priority"]} if r.get("priority") else None,
            "participant": [{"individual": {"display": r["attending"]}}] if r.get("attending") else None,
            "serviceProvider": {"reference": f"Organization/{r.get('source_facility') or r['facility_oid']}"},
            "text": {"status": "generated", "div": f"<div xmlns=\"http://www.w3.org/1999/xhtml\">{_html(r.get('text'))}</div>"}
            if r.get("text") else None}


def _html(s: Optional[str]) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cond(r: dict) -> dict:
    return {"resourceType": "Condition",
            "clinicalStatus": {"coding": [{"system": COND_CLIN, "code": r.get("clinical_status") or "active"}]},
            "verificationStatus": {"coding": [{"system": COND_VER, "code": r["verification"]}]}
            if r.get("verification") else None,
            "category": [{"coding": [{"system": COND_CAT, "code": r.get("category") or "problem-list-item"}]}],
            "severity": {"text": r["severity"]} if r.get("severity") else None,
            "code": cc(r.get("code_system"), r.get("code"), r.get("display"), r.get("text")),
            "subject": _subject(r),
            "encounter": {"reference": f"Encounter/{r['encounter_id']}"} if r.get("encounter_id") else None,
            "onsetDateTime": _fhir_datetime(r.get("onset")),
            "abatementDateTime": _fhir_datetime(r.get("abatement")),
            "note": _note(r)}


def _allergy(r: dict) -> dict:
    sev = (r.get("severity") or "").lower()
    crit = (r.get("criticality") or "").lower()
    return {"resourceType": "AllergyIntolerance",
            "clinicalStatus": {"coding": [{"system": ALLERGY_CLIN,
                                           "code": "inactive" if r.get("status") == "inactive" else "active"}]},
            "category": [r["category"]] if r.get("category") in ("food", "medication", "environment", "biologic") else None,
            "criticality": crit if crit in ("low", "high", "unable-to-assess") else None,
            "code": cc(r.get("code_system"), r.get("code"), r.get("display"), r.get("text")),
            "patient": _subject(r), "onsetDateTime": _fhir_datetime(r.get("onset")),
            "reaction": [_clean({"manifestation": [{"text": r["reaction"]}],
                                 "severity": sev if sev in ("mild", "moderate", "severe") else None})]
            if r.get("reaction") else None,
            "note": _note(r)}


def _dosage_text(r: dict) -> Optional[str]:
    return " ".join(x for x in (r.get("dose"), r.get("route"), r.get("frequency")) if x) or None


def _med(r: dict) -> dict:
    timing = _clean({"code": {"text": r["frequency"]} if r.get("frequency") else None,
                     "repeat": {"frequency": 1, "period": float(r["frequency_hours"]), "periodUnit": "h"}
                     if r.get("frequency_hours") else None})
    dq = _dose_quantity(r.get("dose"))
    dosage = _clean({"extension": [_ext(EXT_DOSE, r["dose"])] if r.get("dose") else None,
                     "text": _dosage_text(r),
                     "route": {"text": r["route"]} if r.get("route") else None,
                     "timing": timing or None,
                     "doseAndRate": [{"doseQuantity": dq}] if dq else None})
    hist = _ext(EXT_DOSE_HISTORY, r.get("_dose_history"))
    med = cc(r.get("code_system"), r.get("code"), r.get("display"), r.get("text")) or {"text": "unknown"}
    if r.get("kind") == "request":
        st = r.get("status") or "active"
        return {"resourceType": "MedicationRequest",
                "status": st if st in ("active", "on-hold", "cancelled", "completed", "entered-in-error",
                                       "stopped", "draft", "unknown") else "active",
                "intent": "order", "medicationCodeableConcept": med, "subject": _subject(r),
                "authoredOn": _fhir_datetime(r.get("start_at")),
                "requester": {"display": r["prescriber"]} if r.get("prescriber") else None,
                "dosageInstruction": [dosage] if dosage else None,
                "dispenseRequest": {"validityPeriod": _clean({"start": _fhir_datetime(r.get("start_at")),
                                                              "end": _fhir_datetime(r.get("end_at"))})}
                if r.get("end_at") else None,
                "reasonCode": [{"text": r["indication"]}] if r.get("indication") else None,
                "extension": [hist] if hist else None,
                "note": _note(r)}
    st = r.get("status") or "active"
    return {"resourceType": "MedicationStatement",
            "status": st if st in ("active", "completed", "entered-in-error", "intended", "stopped",
                                   "on-hold", "unknown", "not-taken") else "active",
            "medicationCodeableConcept": med, "subject": _subject(r),
            "effectivePeriod": _clean({"start": _fhir_datetime(r.get("start_at")),
                                       "end": _fhir_datetime(r.get("end_at"))}) or None,
            "dosage": [dosage] if dosage else None,
            "informationSource": {"display": r["prescriber"]} if r.get("prescriber") else None,
            "reasonCode": [{"text": r["indication"]}] if r.get("indication") else None,
            "extension": [hist] if hist else None,
            "note": _note(r)}


def _obs(r: dict) -> dict:
    res: dict[str, Any] = {
        "resourceType": "Observation", "status": r.get("status") or "final",
        "category": [{"coding": [{"system": OBS_CAT, "code": r["category"]}]}] if r.get("category") else None,
        "code": cc(r.get("code_system"), r.get("code"), r.get("display"), r.get("text")) or {"text": "observation"},
        "subject": _subject(r),
        "encounter": {"reference": f"Encounter/{r['encounter_id']}"} if r.get("encounter_id") else None,
        "effectiveDateTime": _fhir_datetime(r.get("effective")),
        "performer": [{"display": r["performer"]}] if r.get("performer") else None,
        "extension": [_ext(EXT_PANEL, r["panel_id"])] if r.get("panel_id") else None,
        "interpretation": [{"coding": [{"system": OBS_INTERP, "code": r["interpretation"]}]}]
        if r.get("interpretation") else None,
        "referenceRange": [_clean({"low": {"value": r["ref_low"], "unit": r.get("unit")} if r.get("ref_low") is not None else None,
                                   "high": {"value": r["ref_high"], "unit": r.get("unit")} if r.get("ref_high") is not None else None})]
        if (r.get("ref_low") is not None or r.get("ref_high") is not None) else None,
        "note": _note(r)}
    bp = re.fullmatch(r"\s*(\d{2,3})\s*/\s*(\d{2,3})\s*", r.get("value_text") or "")
    if r.get("code") == "85354-9" and bp:
        res["component"] = [
            {"code": cc(LOINC, "8480-6", "Systolic blood pressure"),
             "valueQuantity": {"value": float(bp.group(1)), "unit": "mm[Hg]", "system": UCUM, "code": "mm[Hg]"}},
            {"code": cc(LOINC, "8462-4", "Diastolic blood pressure"),
             "valueQuantity": {"value": float(bp.group(2)), "unit": "mm[Hg]", "system": UCUM, "code": "mm[Hg]"}}]
    elif r.get("value_num") is not None:
        q = {"value": r["value_num"]}
        if r.get("unit"):
            q.update({"unit": r["unit"], "system": UCUM, "code": r["unit"]})
        res["valueQuantity"] = q
    elif r.get("value_text"):
        res["valueString"] = r["value_text"]
    return res


def _proc(r: dict) -> dict:
    st = r.get("status") or "completed"
    return {"resourceType": "Procedure",
            "status": st if st in ("preparation", "in-progress", "not-done", "on-hold", "stopped",
                                   "completed", "entered-in-error", "unknown") else "completed",
            "code": cc(r.get("code_system"), r.get("code"), r.get("display"), r.get("text")),
            "subject": _subject(r), "performedDateTime": _fhir_datetime(r.get("performed")),
            "performer": [{"actor": {"display": r["performer"]}}] if r.get("performer") else None,
            "bodySite": [{"text": r["body_site"]}] if r.get("body_site") else None,
            "outcome": {"text": r["outcome"]} if r.get("outcome") else None, "note": _note(r)}


def _imm(r: dict) -> dict:
    st = r.get("status") or "completed"
    return {"resourceType": "Immunization",
            "status": st if st in ("completed", "entered-in-error", "not-done") else "completed",
            "vaccineCode": cc(r.get("code_system"), r.get("code"), r.get("display"), r.get("text")) or {"text": "vaccine"},
            "patient": _subject(r),
            "occurrenceDateTime": _fhir_datetime(r.get("occurrence")) or _iso(r.get("created_at")),
            "lotNumber": r.get("lot"), "site": {"text": r["site"]} if r.get("site") else None,
            "route": {"text": r["route"]} if r.get("route") else None,
            "performer": [{"actor": {"display": r["performer"]}}] if r.get("performer") else None,
            "protocolApplied": [{"doseNumberString": str(r["dose_number"])}] if r.get("dose_number") else None,
            "note": _note(r)}


def _doc(r: dict) -> dict:
    lo = DOC_TYPES.get(r.get("doc_type") or "")
    content = r.get("content")
    att = _clean({"contentType": r.get("content_type") or "text/plain",
                  "data": base64.b64encode(content.encode()).decode() if isinstance(content, str) else None,
                  "title": r.get("title"), "size": r.get("size_bytes"),
                  "creation": _fhir_datetime(r.get("effective"))})
    st = r.get("status") or "current"
    return {"resourceType": "DocumentReference",
            "status": st if st in ("current", "superseded", "entered-in-error") else "current",
            "type": cc(LOINC, lo[0], lo[1]) if lo else ({"text": r["doc_type"]} if r.get("doc_type") else None),
            "subject": _subject(r), "date": _iso(r.get("created_at")),
            "author": [{"display": r["author"]}] if r.get("author") else None,
            "description": r.get("title"), "content": [{"attachment": att}],
            "context": _clean({"encounter": [{"reference": f"Encounter/{r['encounter_id']}"}] if r.get("encounter_id") else None,
                               "period": {"start": _fhir_datetime(r.get("effective"))} if r.get("effective") else None}) or None}


def _sr(r: dict) -> dict:
    pr_ = (r.get("priority") or "").lower()
    st = r.get("status") or "active"
    return {"resourceType": "ServiceRequest",
            "identifier": [{"type": {"coding": [{"system": V2_0203, "code": "ACSN"}]},
                            "system": f"urn:aranmed:accession:{r.get('source_facility') or r['facility_oid']}",
                            "value": r["accession"]}] if r.get("accession") else None,
            "status": st if st in ("draft", "active", "on-hold", "revoked", "completed", "entered-in-error", "unknown") else "active",
            "intent": r.get("intent") or "order",
            "category": [{"text": r["category"]}] if r.get("category") else None,
            "priority": pr_ if pr_ in ("routine", "urgent", "asap", "stat") else None,
            "code": cc(r.get("code_system"), r.get("code"), r.get("display"), r.get("text")),
            "subject": _subject(r),
            "encounter": {"reference": f"Encounter/{r['encounter_id']}"} if r.get("encounter_id") else None,
            "occurrenceDateTime": _fhir_datetime(r.get("occurrence")),
            "requester": {"display": r["requester"]} if r.get("requester") else None,
            "performer": [{"display": r["performer"]}] if r.get("performer") else None,
            "reasonCode": [{"text": r["reason"]}] if r.get("reason") else None, "note": _note(r)}


def _dr(r: dict) -> dict:
    st = r.get("status") or "final"
    return {"resourceType": "DiagnosticReport",
            "status": st if st in ("registered", "partial", "preliminary", "final", "amended", "corrected",
                                   "appended", "cancelled", "entered-in-error", "unknown") else "final",
            "category": [{"coding": [{"system": DIAG_SVC, "code": r["category"]}]}] if r.get("category") else None,
            "code": cc(r.get("code_system"), r.get("code"), r.get("display"), r.get("text") if not r.get("display") else None)
            or {"text": "report"},
            "subject": _subject(r),
            "encounter": {"reference": f"Encounter/{r['encounter_id']}"} if r.get("encounter_id") else None,
            "effectiveDateTime": _fhir_datetime(r.get("effective")),
            "issued": _instant(r.get("issued")) or _iso(r.get("updated_at")),
            "performer": [{"display": r["performer"]}] if r.get("performer") else None,
            "result": [{"reference": f"Observation/{i}"} for i in r.get("result_ids") or []] or None,
            "imagingStudy": [{"reference": f"ImagingStudy/{r['study_uid']}"}] if r.get("study_uid") else None,
            "conclusion": r.get("conclusion"),
            "presentedForm": [{"contentType": "text/plain",
                               "data": base64.b64encode(r["text"].encode()).decode()}] if r.get("text") else None}


def _consent(r: dict) -> dict:
    cat = r.get("category") or "patient-privacy"
    ptype = {"deny-sharing": "deny", "permit-sharing": "permit", "restricted": "deny"}.get(cat, "permit")
    st = r.get("status") or "active"
    grantee = r.get("grantee")
    return {"resourceType": "Consent",
            "status": st if st in ("draft", "proposed", "active", "rejected", "inactive", "entered-in-error") else "active",
            "scope": {"coding": [{"system": CONSENT_SCOPE, "code": r.get("scope") or "patient-privacy"}]},
            "category": [{"coding": [{"system": CONSENT_CAT, "code": cat}], "text": r.get("text") or cat}],
            "patient": _subject(r), "dateTime": _iso(r.get("created_at")),
            "provision": _clean({
                "type": ptype,
                "period": _clean({"start": _fhir_datetime(r.get("start_at")), "end": _fhir_datetime(r.get("end_at"))}) or None,
                "actor": [{"role": {"text": "grantee"},
                           "reference": {"identifier": {"system": "urn:ietf:rfc:3986",
                                                        "value": f"urn:oid:{grantee}" if grantee and grantee != "*" else "urn:aranmed:any"}}}]
                if grantee else None,
                "purpose": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ActReason", "code": p}
                            for p in r.get("purposes") or []] or None})}


_TO = {"encounter": _enc, "condition": _cond, "allergy": _allergy, "medication": _med,
       "observation": _obs, "procedure": _proc, "immunization": _imm, "document": _doc,
       "service_request": _sr, "diagnostic_report": _dr, "consent": _consent}


# ---------------------------------------------------------------------------
# FHIR -> internal
# ---------------------------------------------------------------------------
def from_fhir(res: dict) -> tuple[str, dict[str, Any]]:
    rt = res.get("resourceType")
    rtype = FHIR_TO_TYPE.get(rt)
    if rtype is None:
        raise ValueError(f"resource type {rt} is not supported for write")
    v: dict[str, Any] = {}
    src_fac, src_id = _parse_source(res)
    if src_fac:
        v["source_facility"], v["source_id"] = src_fac, src_id
    subj = res.get("subject") or res.get("patient")
    v["person_id"] = _ref_id(subj, "Patient")
    enc = res.get("encounter") or {}
    if _ref_id(enc, "Encounter"):
        v["encounter_id"] = _ref_id(enc, "Encounter")
    if res.get("note"):
        v["note"] = "\n".join(n.get("text") or "" for n in res["note"])
    elif _ext_get(res, EXT_NOTE):
        v["note"] = _ext_get(res, EXT_NOTE)
    data = _ext_get(res, EXT_DATA, as_json=True)
    if data:
        v["data"] = data
    if rt == "Encounter":
        per = res.get("period") or {}
        v.update(status=res.get("status"), **{"class": (res.get("class") or {}).get("code")},
                 type_text=((res.get("type") or [{}])[0]).get("text"),
                 reason=((res.get("reasonCode") or [{}])[0]).get("text"),
                 start_at=per.get("start"), end_at=per.get("end"),
                 location=(((res.get("location") or [{}])[0]).get("location") or {}).get("display"),
                 department=(res.get("serviceType") or {}).get("text"),
                 attending=(((res.get("participant") or [{}])[0]).get("individual") or {}).get("display"),
                 text=_strip_div((res.get("text") or {}).get("div")),
                 priority=(res.get("priority") or {}).get("text") or _coding_code(res.get("priority")),
                 admit_source=((res.get("hospitalization") or {}).get("admitSource") or {}).get("text"),
                 disposition=((res.get("hospitalization") or {}).get("dischargeDisposition") or {}).get("text"))
        v.pop("encounter_id", None)
    elif rt == "Condition":
        v.update(_cc_parts(res.get("code")), clinical_status=_coding_code(res.get("clinicalStatus")),
                 verification=_coding_code(res.get("verificationStatus")),
                 category=_coding_code((res.get("category") or [None])[0]),
                 severity=(res.get("severity") or {}).get("text") or _coding_code(res.get("severity")),
                 onset=res.get("onsetDateTime") or res.get("onsetString"),
                 abatement=res.get("abatementDateTime"))
    elif rt == "AllergyIntolerance":
        rx = (res.get("reaction") or [{}])[0]
        v.update(_cc_parts(res.get("code")),
                 status="inactive" if _coding_code(res.get("clinicalStatus")) in ("inactive", "resolved") else "active",
                 category=(res.get("category") or [None])[0], criticality=res.get("criticality"),
                 reaction=((rx.get("manifestation") or [{}])[0]).get("text") or _coding_code((rx.get("manifestation") or [None])[0]),
                 severity=rx.get("severity"), onset=res.get("onsetDateTime"))
    elif rt in ("MedicationStatement", "MedicationRequest"):
        dos = ((res.get("dosage") or res.get("dosageInstruction") or [{}])[0])
        rep = ((dos.get("timing") or {}).get("repeat") or {})
        per = res.get("effectivePeriod") or {}
        dq = ((dos.get("doseAndRate") or [{}])[0]).get("doseQuantity") or {}
        dose = _ext_get(dos, EXT_DOSE)
        if not dose and dq.get("value") is not None:
            dose = f"{dq['value']:g} {dq.get('unit') or ''}".strip()
        timing = dos.get("timing") or {}
        # Older peers only sent the combined dosage text; keep it as frequency.
        freq = (timing.get("code") or {}).get("text") or (None if dose else dos.get("text"))
        vp = (res.get("dispenseRequest") or {}).get("validityPeriod") or {}
        v.update(_cc_parts(res.get("medicationCodeableConcept")), status=res.get("status"),
                 kind="request" if rt == "MedicationRequest" else "statement",
                 dose=dose, frequency=freq, route=(dos.get("route") or {}).get("text"),
                 frequency_hours=rep.get("period") if rep.get("periodUnit") == "h" else None,
                 start_at=per.get("start") or res.get("authoredOn"), end_at=per.get("end") or vp.get("end"),
                 prescriber=(res.get("requester") or res.get("informationSource") or {}).get("display"),
                 indication=((res.get("reasonCode") or [{}])[0]).get("text"))
        hist = _ext_get(res, EXT_DOSE_HISTORY, as_json=True)
        if hist:
            v["_dose_history"] = hist
    elif rt == "Observation":
        q = res.get("valueQuantity") or {}
        rr = (res.get("referenceRange") or [{}])[0]
        v.update(_cc_parts(res.get("code")), status=res.get("status"),
                 category=_coding_code((res.get("category") or [None])[0]),
                 value_num=q.get("value"), unit=q.get("unit") or q.get("code"),
                 value_text=res.get("valueString"),
                 ref_low=(rr.get("low") or {}).get("value"), ref_high=(rr.get("high") or {}).get("value"),
                 interpretation=_coding_code((res.get("interpretation") or [None])[0]),
                 effective=res.get("effectiveDateTime"),
                 performer=((res.get("performer") or [{}])[0]).get("display"),
                 panel_id=_ext_get(res, EXT_PANEL))
        comps = res.get("component") or []
        if comps and v.get("value_num") is None and len(comps) == 2:
            vals = [str(int((c.get("valueQuantity") or {}).get("value", 0))) for c in comps]
            v["value_text"] = "/".join(vals)
            v["unit"] = "mm[Hg]"
    elif rt == "Procedure":
        v.update(_cc_parts(res.get("code")), status=res.get("status"),
                 performed=res.get("performedDateTime"),
                 performer=(((res.get("performer") or [{}])[0]).get("actor") or {}).get("display"),
                 body_site=((res.get("bodySite") or [{}])[0]).get("text"),
                 outcome=(res.get("outcome") or {}).get("text"))
    elif rt == "Immunization":
        v.update(_cc_parts(res.get("vaccineCode")), status=res.get("status"),
                 occurrence=res.get("occurrenceDateTime") or res.get("occurrenceString"),
                 lot=res.get("lotNumber"), site=(res.get("site") or {}).get("text"),
                 route=(res.get("route") or {}).get("text"),
                 performer=(((res.get("performer") or [{}])[0]).get("actor") or {}).get("display"),
                 dose_number=((res.get("protocolApplied") or [{}])[0]).get("doseNumberString"))
    elif rt == "DocumentReference":
        att = ((res.get("content") or [{}])[0]).get("attachment") or {}
        code = _coding_code(res.get("type"))
        data = att.get("data")
        v.update(status=res.get("status"), doc_type=_DOC_BY_LOINC.get(code or "", code),
                 title=res.get("description") or att.get("title"),
                 author=((res.get("author") or [{}])[0]).get("display"),
                 content_type=att.get("contentType"),
                 content=base64.b64decode(data).decode("utf-8", errors="replace") if data else None,
                 effective=((res.get("context") or {}).get("period") or {}).get("start") or att.get("creation"))
        ctx_enc = ((res.get("context") or {}).get("encounter") or [None])[0]
        if _ref_id(ctx_enc, "Encounter"):
            v["encounter_id"] = _ref_id(ctx_enc, "Encounter")
    elif rt == "ServiceRequest":
        acc = next((i.get("value") for i in res.get("identifier") or []
                    if ((i.get("type") or {}).get("coding") or [{}])[0].get("code") == "ACSN"), None)
        v.update(_cc_parts(res.get("code")), status=res.get("status"), intent=res.get("intent"),
                 category=((res.get("category") or [{}])[0]).get("text") or _coding_code((res.get("category") or [None])[0]),
                 priority=res.get("priority"), occurrence=res.get("occurrenceDateTime"),
                 requester=(res.get("requester") or {}).get("display"),
                 performer=((res.get("performer") or [{}])[0]).get("display"),
                 reason=((res.get("reasonCode") or [{}])[0]).get("text"), accession=acc)
    elif rt == "DiagnosticReport":
        pf = (res.get("presentedForm") or [{}])[0]
        study = _ref_id(((res.get("imagingStudy") or [None])[0]), "ImagingStudy")
        v.update(_cc_parts(res.get("code")), status=res.get("status"),
                 category=_coding_code((res.get("category") or [None])[0]),
                 effective=res.get("effectiveDateTime"), issued=res.get("issued"),
                 conclusion=res.get("conclusion"), study_uid=study,
                 performer=((res.get("performer") or [{}])[0]).get("display"),
                 result_ids=[x for x in (_ref_id(r_, "Observation") for r_ in res.get("result") or []) if x] or None,
                 text=base64.b64decode(pf["data"]).decode("utf-8", errors="replace") if pf.get("data") else None)
    elif rt == "Consent":
        prov = res.get("provision") or {}
        cat = None
        for c in res.get("category") or []:
            for cd in c.get("coding") or []:
                if cd.get("system") == CONSENT_CAT:
                    cat = cd.get("code")
        if not cat:
            cat = {"deny": "deny-sharing", "permit": "permit-sharing"}.get(prov.get("type"), "patient-privacy")
        grantee = None
        for a in prov.get("actor") or []:
            val = (((a.get("reference") or {}).get("identifier")) or {}).get("value") or ""
            grantee = val[8:] if val.startswith("urn:oid:") else "*"
        per = prov.get("period") or {}
        v.update(status=res.get("status"), scope=_coding_code(res.get("scope")), category=cat,
                 grantee=grantee, start_at=per.get("start"), end_at=per.get("end"),
                 purposes=[p.get("code") for p in prov.get("purpose") or []],
                 text=((res.get("category") or [{}])[0]).get("text"))
    return rtype, {k: val for k, val in v.items() if val is not None}
