"""Unit: every EHR resource type survives internal row -> FHIR R4 -> internal row.

This is the exchange format between hospitals (transfer packages, the live
federated chart, the FHIR server), so a field lost here is a field a
receiving hospital never sees. Each emitted resource is also validated
against the official FHIR R4B models.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_780_000_000.0

BASE = {"person_id": "p1", "facility_oid": "2.25.9", "source_facility": "2.25.9", "version": 1,
        "created_at": NOW, "updated_at": NOW, "note": "free-text note"}

ROWS = {
    "encounter": {"class": "IMP", "type_text": "Admission for PCI", "reason": "NSTEMI",
                  "start_at": "2026-04-10T22:00:00Z", "end_at": "2026-04-14T12:00:00Z",
                  "location": "Ward 3", "department": "CCU", "attending": "Dr. Kazemi",
                  "admit_source": "emergency", "disposition": "home", "priority": "urgent",
                  "status": "finished", "text": "Admitted via ED"},
    "condition": {"display": "Coronary artery disease", "code_system": "http://snomed.info/sct",
                  "code": "53741008", "clinical_status": "active", "verification": "confirmed",
                  "category": "problem-list-item", "severity": "moderate", "onset": "2025-01-01",
                  "abatement": "2026-01-01", "encounter_id": "e1"},
    "allergy": {"display": "Iodinated contrast", "reaction": "Anaphylaxis", "severity": "severe",
                "criticality": "high", "category": "medication", "onset": "2019-02-02", "status": "active"},
    "medication": {"display": "Metformin", "kind": "statement", "dose": "1000 mg", "route": "PO",
                   "frequency": "three times daily", "frequency_hours": 8.0, "status": "active",
                   "start_at": "2015-06-01", "end_at": "2027-01-01", "prescriber": "Dr. Amini",
                   "indication": "Type 2 diabetes", "data": {"pharmacy": "outpatient"}},
    "medication_request": {"display": "Clopidogrel", "kind": "request", "dose": "75 mg", "route": "PO",
                           "frequency": "daily", "frequency_hours": 24.0, "status": "active",
                           "start_at": "2026-04-14", "end_at": "2027-04-14", "prescriber": "Dr. Kazemi",
                           "indication": "after PCI"},
    "observation": {"display": "Creatinine", "code_system": "http://loinc.org", "code": "2160-0",
                    "category": "laboratory", "value_num": 1.9, "unit": "mg/dL", "ref_low": 0.6,
                    "ref_high": 1.2, "interpretation": "H", "effective": "2026-04-13",
                    "performer": "Central Lab", "status": "final", "encounter_id": "e1", "panel_id": "panel-7"},
    "procedure": {"display": "PCI", "code_system": "http://snomed.info/sct", "code": "415070008",
                  "performed": "2026-04-11", "performer": "Dr. Kazemi", "body_site": "LAD",
                  "outcome": "successful", "status": "completed"},
    "immunization": {"display": "Influenza vaccine", "occurrence": "2025-10-01", "lot": "FLU-77",
                     "dose_number": "1", "site": "left deltoid", "route": "IM", "performer": "Nurse Rahimi",
                     "status": "completed"},
    "document": {"doc_type": "discharge-summary", "title": "Discharge after PCI", "author": "Dr. Kazemi",
                 "content_type": "text/plain", "content": "Discharged on DAPT.\nخلاصه ترخیص",
                 "effective": "2026-04-14", "status": "current", "encounter_id": "e1"},
    "service_request": {"display": "Cardiac MRI", "category": "imaging", "intent": "order",
                        "priority": "routine", "reason": "viability", "requester": "Dr. Kazemi",
                        "performer": "Radiology", "accession": "ACC-1", "occurrence": "2026-05-01T09:00:00Z",
                        "status": "active", "data": {"modality": "MR"}},
    "diagnostic_report": {"display": "Renal panel", "category": "LAB", "effective": "2026-04-13",
                          "issued": "2026-04-13T10:00:00Z", "conclusion": "Rising creatinine",
                          "study_uid": "1.2.3.4", "performer": "Central Lab", "status": "final",
                          "result_ids": ["o1", "o2"], "text": "Full report text"},
    "consent": {"category": "deny-sharing", "grantee": "2.25.10", "status": "active",
                "scope": "patient-privacy", "purposes": ["TREAT"], "start_at": "2026-01-01",
                "end_at": "2027-01-01", "text": "Declines sharing"},
}
IGNORE = {"code_system"}  # only when no code is given


def _row(key: str) -> tuple[str, dict]:
    rtype = "medication" if key.startswith("medication") else key
    rid = "id-" + key.replace("_", "-")
    return rtype, {**BASE, "id": rid, "source_id": rid, **ROWS[key]}


def _norm(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str) and v.endswith("Z"):
        return v[:-1]
    return v


@pytest.mark.parametrize("key", list(ROWS))
def test_row_fhir_row_is_lossless(key):
    from interop import fhir_map
    rtype, row = _row(key)
    res = fhir_map.to_fhir(rtype, row)
    back_type, back = fhir_map.from_fhir(res)
    assert back_type == rtype
    assert back["source_facility"] == "2.25.9" and back["source_id"] == row["source_id"]
    assert back["person_id"] == "p1"
    for field, want in ROWS[key].items():
        assert _norm(back.get(field)) == _norm(want), (key, field, want, back.get(field))
    assert back.get("note") == "free-text note"


def test_medication_dose_history_and_legacy_peer_text():
    from interop import fhir_map
    _, row = _row("medication")
    hist = [{"version": 1, "dose": "500 mg", "frequency_hours": 12}, {"version": 2, "dose": "1000 mg"}]
    res = fhir_map.to_fhir("medication", {**row, "_dose_history": hist})
    assert res["dosage"][0]["doseAndRate"][0]["doseQuantity"] == {
        "value": 1000.0, "unit": "mg", "system": "http://unitsofmeasure.org", "code": "mg"}
    assert fhir_map.from_fhir(res)[1]["_dose_history"] == hist
    # A peer that only sends dosage.text (older systems) keeps that text.
    legacy = {"resourceType": "MedicationStatement", "status": "active", "subject": {"reference": "Patient/p1"},
              "medicationCodeableConcept": {"text": "Aspirin"}, "dosage": [{"text": "81 mg PO daily"}]}
    vals = fhir_map.from_fhir(legacy)[1]
    assert vals["frequency"] == "81 mg PO daily" and "dose" not in vals
    # A peer that sends only a structured quantity gets a readable dose.
    q = {**legacy, "dosage": [{"doseAndRate": [{"doseQuantity": {"value": 2.5, "unit": "mg"}}]}]}
    assert fhir_map.from_fhir(q)[1]["dose"] == "2.5 mg"


def test_patient_round_trip_keeps_aliases_language_deceased():
    from interop import fhir_map
    person = {"id": "p1", "status": "active", "home_facility": "2.25.9", "updated_at": NOW,
              "identifiers": [{"system": "urn:oid:2.25.9", "value": "MRN-1", "type": "MR", "facility_oid": "2.25.9"}],
              "demographics": {"family": "Ahmadi", "given": "Dariush", "sex": "male", "birth_date": "1966-02-03",
                               "phone": "+98-21-555", "language": "fa", "deceased": False,
                               "aliases": [{"family": "Ahmadi-Nejad", "given": "Dariush"}]}}
    demo, idents = fhir_map.patient_in(fhir_map.patient(person))
    assert demo["aliases"] == [{"family": "Ahmadi-Nejad", "given": "Dariush"}]
    assert demo["language"] == "fa" and demo["deceased"] is False and demo["phone"] == "+98-21-555"
    assert idents[0]["value"] == "MRN-1" and idents[0]["facility_oid"] == "2.25.9"


def test_every_emitted_resource_is_valid_fhir_r4b(tmp_path):
    from interop import fhir_map
    out = [fhir_map.to_fhir(*_row(k)) for k in ROWS]
    f = tmp_path / "resources.json"
    f.write_text(json.dumps(out), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(ROOT / "tests/functional/fhir_validate.py"), str(f)],
                          capture_output=True, text=True, cwd=str(tmp_path))
    result = json.loads(proc.stdout)
    assert result["errors"] == [] and result["ok"] == len(out), result["errors"]
