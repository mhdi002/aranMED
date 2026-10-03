"""Functional: the FHIR R4 server — CRUD, search, transactions, operations —
with every emitted resource validated against the official R4B models."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.functional.dicom_factory import make_study, to_bytes

F = "/api/fhir/r4"
HERE = Path(__file__).resolve().parent
EMITTED: list[dict] = []


def _keep(res: dict) -> dict:
    if res.get("resourceType") == "Bundle":
        for e in res.get("entry") or []:
            if e.get("resource"):
                EMITTED.append(e["resource"])
    EMITTED.append(res)
    return res


def validate(resources: list[dict], tmp_path) -> dict:
    f = tmp_path / "fhir.json"
    f.write_text(json.dumps(resources))
    root = HERE.parents[1]
    out = subprocess.run([sys.executable, str(HERE / "fhir_validate.py"), str(f)],
                         capture_output=True, text=True, cwd=str(root / "tests"),
                         env={"PYTHONPATH": ""})
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _txn(patient_ref="urn:uuid:p1"):
    pat = {"resourceType": "Patient", "identifier": [
        {"system": "urn:aranmed:national-id", "value": "1234567890",
         "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "NI"}]}},
        {"system": "urn:oid:2.25.1001", "value": "F-100"}],
        "name": [{"family": "Ahmadi", "given": ["Parisa"]}], "gender": "female",
        "birthDate": "1979-11-02", "telecom": [{"system": "phone", "value": "+98-21-555"}]}
    subj = {"reference": patient_ref}
    res = [
        ("Patient", pat, "urn:uuid:p1"),
        ("Encounter", {"resourceType": "Encounter", "status": "finished",
                       "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "IMP"},
                       "subject": subj, "period": {"start": "2026-09-01T10:00:00Z", "end": "2026-09-04T12:00:00Z"},
                       "reasonCode": [{"text": "Community acquired pneumonia"}]}, "urn:uuid:e1"),
        ("Condition", {"resourceType": "Condition",
                       "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
                       "code": {"coding": [{"system": "http://snomed.info/sct", "code": "38341003", "display": "Hypertension"}]},
                       "subject": subj, "encounter": {"reference": "urn:uuid:e1"}, "onsetDateTime": "2015-01-01"}, None),
        ("AllergyIntolerance", {"resourceType": "AllergyIntolerance", "patient": subj,
                                "code": {"text": "Penicillin"}, "criticality": "high",
                                "reaction": [{"manifestation": [{"text": "Anaphylaxis"}], "severity": "severe"}]}, None),
        ("MedicationStatement", {"resourceType": "MedicationStatement", "status": "active", "subject": subj,
                                 "medicationCodeableConcept": {"text": "Amlodipine"},
                                 "dosage": [{"text": "5 mg oral daily", "timing": {"repeat": {"frequency": 1, "period": 24, "periodUnit": "h"}}}]}, None),
        ("Observation", {"resourceType": "Observation", "status": "final", "subject": subj,
                         "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
                         "code": {"coding": [{"system": "http://loinc.org", "code": "2160-0", "display": "Creatinine"}]},
                         "valueQuantity": {"value": 1.9, "unit": "mg/dL"}, "effectiveDateTime": "2026-09-02T08:00:00Z",
                         "referenceRange": [{"low": {"value": 0.6}, "high": {"value": 1.2}}]}, None),
        ("Observation", {"resourceType": "Observation", "status": "final", "subject": subj,
                         "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs"}]}],
                         "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
                         "component": [{"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]}, "valueQuantity": {"value": 150}},
                                       {"code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]}, "valueQuantity": {"value": 95}}],
                         "effectiveDateTime": "2026-09-02T08:00:00Z"}, None),
        ("Procedure", {"resourceType": "Procedure", "status": "completed", "subject": subj,
                       "code": {"text": "Chest X-ray"}, "performedDateTime": "2026-09-01"}, None),
        ("Immunization", {"resourceType": "Immunization", "status": "completed", "patient": subj,
                          "vaccineCode": {"text": "Tetanus"}, "occurrenceDateTime": "2024-05-01"}, None),
        ("DocumentReference", {"resourceType": "DocumentReference", "status": "current", "subject": subj,
                               "type": {"coding": [{"system": "http://loinc.org", "code": "18842-5"}]},
                               "description": "Discharge summary",
                               "content": [{"attachment": {"contentType": "text/plain",
                                                           "data": "RGlzY2hhcmdlZCBob21lIG9uIG9yYWwgYW50aWJpb3RpY3Mu"}}]}, None),
        ("ServiceRequest", {"resourceType": "ServiceRequest", "status": "active", "intent": "order",
                            "category": [{"text": "imaging"}], "priority": "urgent", "subject": subj,
                            "code": {"text": "CT chest follow-up"}, "reasonCode": [{"text": "Pneumonia follow-up"}]}, None),
    ]
    entries = []
    for rt, r, full in res:
        e = {"resource": r, "request": {"method": "POST", "url": rt}}
        if full:
            e["fullUrl"] = full
        entries.append(e)
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def test_fhir_server_end_to_end(client, users, tmp_path):
    doc = users["doctor"]
    H = {**doc, "Content-Type": "application/fhir+json"}
    cap = _keep(client.get(f"{F}/metadata").json())
    assert cap["fhirVersion"] == "4.0.1"
    assert any(o["name"] == "everything" for o in cap["rest"][0]["resource"][0]["operation"])

    # --- transaction with urn:uuid references -------------------------------------
    r = client.post(F, headers=H, json=_txn())
    assert r.status_code == 200, r.text
    resp = _keep(r.json())
    assert resp["type"] == "transaction-response"
    assert all(e["response"]["status"] in ("200", "201") for e in resp["entry"])
    pid = resp["entry"][0]["resource"]["id"]
    enc_id = resp["entry"][1]["resource"]["id"]
    cond = resp["entry"][2]["resource"]
    assert cond["subject"]["reference"] == f"Patient/{pid}"
    assert cond["encounter"]["reference"] == f"Encounter/{enc_id}"

    # --- search -------------------------------------------------------------------------
    b = client.get(f"{F}/Patient", params={"identifier": "urn:aranmed:national-id|1234567890"}, headers=doc).json()
    assert b["total"] == 1 and b["entry"][0]["resource"]["id"] == pid
    _keep(b)
    b = client.get(f"{F}/Observation", headers=doc, params={
        "patient": f"Patient/{pid}", "category": "laboratory", "code": "http://loinc.org|2160-0",
        "date": "ge2026-09-01"}).json()
    assert b["total"] == 1
    obs = b["entry"][0]["resource"]
    assert obs["valueQuantity"]["value"] == 1.9
    assert obs["interpretation"][0]["coding"][0]["code"] == "H"  # flagged from the range
    assert client.get(f"{F}/Observation", headers=doc, params={
        "patient": pid, "date": "lt2026-01-01"}).json()["total"] == 0
    bp = client.get(f"{F}/Observation", headers=doc, params={"patient": pid, "code": "85354-9"}).json()
    comp = bp["entry"][0]["resource"]["component"]
    assert [c["valueQuantity"]["value"] for c in comp] == [150.0, 95.0]
    _keep(bp)
    for rt in ("Encounter", "Condition", "AllergyIntolerance", "MedicationStatement", "Procedure",
               "Immunization", "DocumentReference", "ServiceRequest"):
        bb = client.get(f"{F}/{rt}", params={"patient": pid}, headers=doc).json()
        assert bb["total"] >= 1, rt
        _keep(bb)
    # The imaging order reached the modality worklist and carries its accession.
    sr = client.get(f"{F}/ServiceRequest", params={"patient": pid}, headers=doc).json()["entry"][0]["resource"]
    assert sr["identifier"][0]["type"]["coding"][0]["code"] == "ACSN"
    acc = sr["identifier"][0]["value"]
    assert any(w["accession"] == acc for w in client.get("/api/pacs/worklist", headers=doc).json()["items"])

    # --- read / update / versioning ---------------------------------------------------
    r = client.get(f"{F}/Condition/{cond['id']}", headers=doc)
    assert r.headers["etag"] == 'W/"1"'
    cond2 = {**r.json(), "severity": {"text": "moderate"}}
    r = client.put(f"{F}/Condition/{cond['id']}", headers={**H, "If-Match": 'W/"1"'}, json=cond2)
    assert r.status_code == 200 and r.json()["meta"]["versionId"] == "2"
    stale = client.put(f"{F}/Condition/{cond['id']}", headers={**H, "If-Match": 'W/"1"'}, json=cond2)
    assert stale.status_code == 412 and stale.json()["resourceType"] == "OperationOutcome"
    hist = _keep(client.get(f"{F}/Condition/{cond['id']}/_history", headers=doc).json())
    assert hist["type"] == "history" and hist["total"] == 2
    v1 = client.get(f"{F}/Condition/{cond['id']}/_history/1", headers=doc).json()
    assert "severity" not in v1
    assert client.delete(f"{F}/Condition/{cond['id']}", headers=doc).status_code == 204
    assert client.get(f"{F}/Condition/{cond['id']}", headers=doc).status_code == 404

    # --- imaging joins $everything --------------------------------------------------------
    from pacs import index
    study_uid, dsets = make_study(n_series=2, per_series=2, patient_name="Ahmadi^Parisa",
                                  patient_id="F-100", birth_date="19791102", sex="F")
    for d in dsets:
        index.ingest(to_bytes(d), source="test")
    ev = _keep(client.get(f"{F}/Patient/{pid}/$everything", headers=doc).json())
    types = [e["resource"]["resourceType"] for e in ev["entry"]]
    assert {"Patient", "Encounter", "AllergyIntolerance", "MedicationStatement", "Observation",
            "ImagingStudy", "Endpoint", "Organization"} <= set(types)
    ims = next(e["resource"] for e in ev["entry"] if e["resource"]["resourceType"] == "ImagingStudy")
    assert ims["id"] == study_uid and ims["numberOfSeries"] == 2 and len(ims["series"]) == 2

    summary = _keep(client.get(f"{F}/Patient/{pid}/$summary", headers=doc).json())
    assert summary["type"] == "document"
    comp = summary["entry"][0]["resource"]
    assert comp["resourceType"] == "Composition"
    assert {s["title"] for s in comp["section"]} >= {"Allergies and intolerances", "Medication summary"}

    # --- PDQm $match and PIXm ---------------------------------------------------------------
    m = client.post(f"{F}/Patient/$match", headers=H, json={"resourceType": "Parameters", "parameter": [
        {"name": "resource", "resource": {"resourceType": "Patient", "name": [{"family": "Ahmadi", "given": ["Parisa"]}],
                                          "birthDate": "1979-11-02", "gender": "female"}}]}).json()
    assert m["entry"][0]["resource"]["id"] == pid
    assert m["entry"][0]["search"]["extension"][0]["valueCode"] in ("probable", "certain")
    _keep(m)
    pix = client.get(f"{F}/Patient/$ihe-pix", headers=doc,
                     params={"sourceIdentifier": "urn:aranmed:national-id|1234567890"}).json()
    vals = {p["valueIdentifier"]["value"] for p in pix["parameter"] if p["name"] == "targetIdentifier"}
    assert "F-100" in vals
    _keep(pix)

    # --- failures: OperationOutcome, rollback, auth -----------------------------------------
    bad = _txn()
    bad["entry"][0]["resource"]["identifier"][0]["value"] = "999"
    bad["entry"][0]["resource"]["identifier"][1]["value"] = "F-999"
    bad["entry"][0]["resource"]["name"] = [{"family": "Rollback", "given": ["Case"]}]
    bad["entry"][-1]["resource"]["subject"] = {"reference": "Patient/does-not-exist"}
    n_before = client.get(f"{F}/Encounter", params={"patient": pid}, headers=doc).json()["total"]
    r = client.post(F, headers=H, json=bad)
    assert r.status_code == 422 and r.json()["resourceType"] == "OperationOutcome"
    # The new patient's encounter created earlier in the same transaction was rolled back.
    p2 = client.get(f"{F}/Patient", params={"identifier": "urn:aranmed:national-id|999"}, headers=doc).json()
    assert p2["total"] == 1  # identity registration is kept (MPI), clinical rows are not
    if p2["total"]:
        assert client.get(f"{F}/Encounter", params={"patient": p2["entry"][0]["resource"]["id"]},
                          headers=doc).json()["total"] == 0
    assert client.get(f"{F}/Encounter", params={"patient": pid}, headers=doc).json()["total"] == n_before
    r = client.get(f"{F}/Patient/{pid}")
    assert r.status_code == 401 and r.json()["resourceType"] == "OperationOutcome"
    assert client.post(f"{F}/Observation", headers={**users["student"], "Content-Type": "application/fhir+json"},
                       json={}).status_code == 403

    # --- conformance: every resource we emitted validates against FHIR R4B -------------------
    unique = {(r["resourceType"], r.get("id")): r for r in EMITTED if r.get("resourceType")}
    result = validate(list(unique.values()), tmp_path)
    assert result["errors"] == [], json.dumps(result["errors"], indent=1)[:3000]
    assert result["ok"] >= 25
