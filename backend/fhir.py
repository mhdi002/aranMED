"""FHIR R4 projection of AranMed's internal EHR record.

The platform's internal model (see ``tools/ehr.py::EHRRecordSchema``) is
deliberately simple and shaped by what the dictation/agent pipeline actually
produces. This module translates it, read-only, into the FHIR R4 resources an
external system expects — so "standardized data model" means something
concrete at the integration boundary without forcing every internal component
to speak FHIR.

Scope, stated plainly:

* **Read-only projection.** There is no FHIR write path, no FHIR-native
  storage, and no full RESTful FHIR server. ``GET /api/fhir/Patient/{id}``
  and ``$everything`` are what exist.
* **Mapped resources:** Patient, Condition, AllergyIntolerance,
  MedicationStatement, Observation (vitals).
* **Terminology is partial.** Vitals carry proper LOINC codes because those
  are stable and few. Conditions, allergies and medications are emitted as
  ``text``-only ``CodeableConcept``s — the source is dictated free text, and
  inventing SNOMED/RxNorm codes from it would fabricate clinical precision
  the data does not have. A terminology-server binding is the correct fix and
  is not attempted here.

FHIR R4 spec: https://hl7.org/fhir/R4/
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

# LOINC codes for the vitals we capture. Small, stable, unambiguous.
_VITAL_LOINC: dict[str, tuple[str, str, str]] = {
    # key: (LOINC code, display, UCUM unit)
    "hr":     ("8867-4",  "Heart rate",                  "/min"),
    "rr":     ("9279-1",  "Respiratory rate",            "/min"),
    "temp_c": ("8310-5",  "Body temperature",            "Cel"),
    "spo2":   ("59408-5", "Oxygen saturation in Arterial blood by Pulse oximetry", "%"),
}
_BP_PANEL = ("85354-9", "Blood pressure panel with all children optional")
_BP_SYSTOLIC = ("8480-6", "Systolic blood pressure")
_BP_DIASTOLIC = ("8462-4", "Diastolic blood pressure")

_SEX_MAP = {
    "m": "male", "male": "male", "مرد": "male",
    "f": "female", "female": "female", "زن": "female",
    "o": "other", "other": "other",
    "u": "unknown", "unknown": "unknown",
}


def _stable_id(*parts: Any) -> str:
    """Deterministic resource id, so repeated exports are idempotent.

    FHIR ids must be stable across reads for a client to reconcile them; a
    random id per request would make every export look like new data.
    """
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]  # noqa: S324


def _text(value: Optional[str]) -> Optional[dict]:
    """A text-only CodeableConcept. See the terminology note in the module docstring."""
    if not value:
        return None
    return {"text": str(value)}


def to_patient(record: dict, patient_id: str) -> dict:
    """The ``Patient`` resource."""
    p = record.get("patient") or {}
    resource: dict[str, Any] = {
        "resourceType": "Patient",
        "id": patient_id,
        "active": True,
    }

    mrn = p.get("mrn")
    if mrn:
        resource["identifier"] = [{
            "use": "usual",
            "type": {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                "code": "MR",
                "display": "Medical Record Number",
            }]},
            "value": str(mrn),
        }]

    name = p.get("name")
    if name:
        resource["name"] = [{"use": "official", "text": str(name)}]

    sex = (p.get("sex") or "").strip().lower()
    if sex:
        resource["gender"] = _SEX_MAP.get(sex, "unknown")

    # The internal model records age, not date of birth. Emitting a computed
    # birthDate would invent precision that isn't in the source, so age goes
    # in an extension that says exactly what it is.
    age = p.get("age")
    if age is not None:
        resource["extension"] = [{
            "url": "http://hl7.org/fhir/StructureDefinition/patient-age-reported",
            "valueQuantity": {
                "value": age, "unit": "a",
                "system": "http://unitsofmeasure.org", "code": "a",
            },
        }]
    return resource


def to_conditions(record: dict, patient_id: str) -> list[dict]:
    out = []
    for prob in record.get("problems") or []:
        name = prob.get("name")
        if not name:
            continue
        res: dict[str, Any] = {
            "resourceType": "Condition",
            "id": _stable_id(patient_id, "condition", name),
            "subject": {"reference": f"Patient/{patient_id}"},
            "code": _text(name),
        }
        status = (prob.get("status") or "").strip().lower()
        if status in ("active", "resolved"):
            res["clinicalStatus"] = {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                "code": status,
            }]}
        out.append(res)
    return out


def to_allergies(record: dict, patient_id: str) -> list[dict]:
    out = []
    for a in record.get("allergies") or []:
        substance = a.get("substance")
        if not substance:
            continue
        res: dict[str, Any] = {
            "resourceType": "AllergyIntolerance",
            "id": _stable_id(patient_id, "allergy", substance),
            "patient": {"reference": f"Patient/{patient_id}"},
            "code": _text(substance),
            "clinicalStatus": {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                "code": "active",
            }]},
        }
        reaction = a.get("reaction")
        if reaction:
            res["reaction"] = [{"manifestation": [_text(reaction)]}]
        out.append(res)
    return out


def to_medication_statements(record: dict, patient_id: str) -> list[dict]:
    out = []
    for m in record.get("medications") or []:
        name = m.get("name")
        if not name:
            continue
        res: dict[str, Any] = {
            "resourceType": "MedicationStatement",
            "id": _stable_id(patient_id, "medication", name),
            "status": "active",
            "subject": {"reference": f"Patient/{patient_id}"},
            "medicationCodeableConcept": _text(name),
        }

        dosage: dict[str, Any] = {}
        parts = [m.get("dose"), m.get("frequency")]
        text = " ".join(str(x) for x in parts if x)
        if text:
            dosage["text"] = text
        if m.get("route"):
            dosage["route"] = _text(m["route"])

        # frequency_hours is validated as a number upstream, so it maps
        # cleanly onto a real FHIR timing rather than staying prose.
        fh = m.get("frequency_hours")
        if fh:
            try:
                dosage["timing"] = {"repeat": {
                    "frequency": 1, "period": float(fh), "periodUnit": "h",
                }}
            except (TypeError, ValueError):
                pass
        if dosage:
            res["dosage"] = [dosage]

        if m.get("indication"):
            res["reasonCode"] = [_text(m["indication"])]
        if m.get("notes"):
            res["note"] = [{"text": str(m["notes"])}]
        out.append(res)
    return out


def to_observations(record: dict, patient_id: str) -> list[dict]:
    """Vitals as ``Observation`` resources, LOINC-coded."""
    vitals = record.get("vitals") or {}
    out: list[dict] = []

    def _base(obs_id: str, code: str, display: str) -> dict:
        return {
            "resourceType": "Observation",
            "id": obs_id,
            "status": "final",
            "category": [{"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                "code": "vital-signs", "display": "Vital Signs",
            }]}],
            "code": {"coding": [{"system": "http://loinc.org",
                                 "code": code, "display": display}],
                     "text": display},
            "subject": {"reference": f"Patient/{patient_id}"},
        }

    for key, (code, display, unit) in _VITAL_LOINC.items():
        val = vitals.get(key)
        if val is None:
            continue
        try:
            numeric = float(val)
        except (TypeError, ValueError):
            continue
        res = _base(_stable_id(patient_id, "obs", key), code, display)
        res["valueQuantity"] = {"value": numeric, "unit": unit,
                                "system": "http://unitsofmeasure.org", "code": unit}
        out.append(res)

    # Blood pressure is a panel with two components, not one scalar.
    bp = vitals.get("bp")
    if bp and isinstance(bp, str) and "/" in bp:
        try:
            sys_s, dia_s = bp.split("/", 1)
            systolic, diastolic = float(sys_s.strip()), float(dia_s.strip().split()[0])
        except (ValueError, IndexError):
            systolic = diastolic = None
        if systolic is not None and diastolic is not None:
            res = _base(_stable_id(patient_id, "obs", "bp"), *_BP_PANEL)
            res["component"] = [
                {"code": {"coding": [{"system": "http://loinc.org",
                                      "code": _BP_SYSTOLIC[0], "display": _BP_SYSTOLIC[1]}]},
                 "valueQuantity": {"value": systolic, "unit": "mm[Hg]",
                                   "system": "http://unitsofmeasure.org", "code": "mm[Hg]"}},
                {"code": {"coding": [{"system": "http://loinc.org",
                                      "code": _BP_DIASTOLIC[0], "display": _BP_DIASTOLIC[1]}]},
                 "valueQuantity": {"value": diastolic, "unit": "mm[Hg]",
                                   "system": "http://unitsofmeasure.org", "code": "mm[Hg]"}},
            ]
            out.append(res)
    return out


def to_bundle(record: dict, patient_id: str) -> dict:
    """Everything known about the patient, as a FHIR ``searchset`` Bundle."""
    resources: list[dict] = [to_patient(record, patient_id)]
    resources += to_conditions(record, patient_id)
    resources += to_allergies(record, patient_id)
    resources += to_medication_statements(record, patient_id)
    resources += to_observations(record, patient_id)
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": r} for r in resources],
    }
