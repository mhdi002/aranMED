"""Functional: C-CDA export/import and EMS pre-arrival notifications."""
from __future__ import annotations

import os

from lxml import etree

NS = {"v3": "urn:hl7-org:v3"}

EXTERNAL_CCD = b"""<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <typeId root="2.16.840.1.113883.1.3" extension="POCD_HD000040"/>
  <templateId root="2.16.840.1.113883.10.20.22.1.2"/>
  <id root="1.2.3.4" extension="ext-ccd-1"/>
  <code code="34133-9" codeSystem="2.16.840.1.113883.6.1"/>
  <title>Outside hospital CCD</title>
  <effectiveTime value="20260901"/>
  <recordTarget><patientRole>
    <id root="2.25.7777" extension="OUT-55"/>
    <patient><name><given>Omid</given><family>Shams</family></name>
      <administrativeGenderCode code="M" codeSystem="2.16.840.1.113883.5.1"/>
      <birthTime value="19720315"/></patient>
  </patientRole></recordTarget>
  <custodian><assignedCustodian><representedCustodianOrganization>
    <id root="2.25.7777"/><name>Outside Hospital</name>
  </representedCustodianOrganization></assignedCustodian></custodian>
  <component><structuredBody>
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/><title>Problems</title><text>CKD</text>
      <entry><observation classCode="OBS" moodCode="EVN"><id root="2.25.7777" extension="prob-1"/>
        <code code="55607006" codeSystem="2.16.840.1.113883.6.96"/>
        <effectiveTime><low value="20200101"/></effectiveTime>
        <value xsi:type="CD" code="709044004" codeSystem="2.16.840.1.113883.6.96" displayName="Chronic kidney disease"/>
      </observation></entry>
    </section></component>
    <component><section>
      <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/><title>Results</title><text>Cr</text>
      <entry><observation classCode="OBS" moodCode="EVN"><id root="2.25.7777" extension="res-1"/>
        <code code="2160-0" codeSystem="2.16.840.1.113883.6.1" displayName="Creatinine"/>
        <effectiveTime value="20260815"/>
        <value xsi:type="PQ" value="2.4" unit="mg/dL"/>
        <interpretationCode code="H" codeSystem="2.16.840.1.113883.5.83"/>
      </observation></entry>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>"""

NEMSIS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<EMSDataSet xmlns="http://www.nemsis.org">
 <Header><PatientCareReport>
  <eResponse><eResponse.AgencyGroup><eResponse.01>AGENCY-12</eResponse.01></eResponse.AgencyGroup>
   <eResponse.03>INC-2026-0042</eResponse.03><eResponse.14>MEDIC-7</eResponse.14></eResponse>
  <eTimes><eTimes.11>2026-10-03T21:15:00</eTimes.11></eTimes>
  <ePatient><ePatient.PatientNameGroup><ePatient.02>Navabi</ePatient.02><ePatient.03>Kamran</ePatient.03></ePatient.PatientNameGroup>
   <ePatient.12>5566778899</ePatient.12><ePatient.13>9906003</ePatient.13><ePatient.17>1961-07-09</ePatient.17></ePatient>
  <eSituation><eSituation.04>Chest pain</eSituation.04><eSituation.11>STEMI suspected</eSituation.11><eSituation.13>2813001</eSituation.13></eSituation>
  <eVitals><eVitals.VitalGroup><eVitals.01>2026-10-03T21:01:00</eVitals.01>
   <eVitals.BloodPressureGroup><eVitals.06>88</eVitals.06><eVitals.07>54</eVitals.07></eVitals.BloodPressureGroup>
   <eVitals.HeartRateGroup><eVitals.10>118</eVitals.10></eVitals.HeartRateGroup>
   <eVitals.12>91</eVitals.12><eVitals.14>24</eVitals.14><eVitals.GlasgowScoreGroup><eVitals.23>15</eVitals.23></eVitals.GlasgowScoreGroup>
  </eVitals.VitalGroup></eVitals>
  <eMedications><eMedications.MedicationGroup><eMedications.03>Aspirin 300 mg</eMedications.03></eMedications.MedicationGroup>
   <eMedications.MedicationGroup><eMedications.03>Nitroglycerin SL</eMedications.03></eMedications.MedicationGroup></eMedications>
  <eProcedures><eProcedures.ProcedureGroup><eProcedures.03>12-lead ECG</eProcedures.03></eProcedures.ProcedureGroup></eProcedures>
  <eNarrative><eNarrative.01>ST elevation V2-V4. Cath lab activation requested.</eNarrative.01></eNarrative>
 </PatientCareReport></Header>
</EMSDataSet>"""


def test_ccd_export_and_import(client, users):
    doc = users["doctor"]
    reg = client.post("/api/clinical/patients", headers=doc, json={
        "demographics": {"family": "Bahrami", "given": "Laleh", "birth_date": "1992-06-30", "sex": "female"},
        "mrn": "CDA-1", "national_id": "4455667788"}).json()
    pid = reg["person_id"]
    base = f"/api/clinical/patients/{pid}"
    client.post(f"{base}/conditions", headers=doc, json={"display": "Asthma", "code_system": "http://snomed.info/sct",
                                                         "code": "195967001", "clinical_status": "active"})
    client.post(f"{base}/allergies", headers=doc, json={"display": "Aspirin", "reaction": "Bronchospasm"})
    client.post(f"{base}/medications", headers=doc, json={"display": "Salbutamol", "dose": "100 mcg", "route": "inhaled"})
    client.post(f"{base}/observations", headers=doc, json={
        "category": "laboratory", "code_system": "http://loinc.org", "code": "718-7", "display": "Hemoglobin",
        "value_num": 10.1, "unit": "g/dL", "ref_low": 12, "ref_high": 16, "effective": "2026-09-30"})

    r = client.get(f"/api/interop/cda/{pid}", headers=doc)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    root = etree.fromstring(r.content)
    assert root.find("v3:templateId[@root='2.16.840.1.113883.10.20.22.1.2']", NS) is not None
    ids = {(i.get("root"), i.get("extension")) for i in root.findall(".//v3:recordTarget/v3:patientRole/v3:id", NS)}
    assert ("2.25.1001", "CDA-1") in ids
    sections = {s.findtext("v3:title", namespaces=NS): s
                for s in root.findall(".//v3:structuredBody/v3:component/v3:section", NS)}
    assert {"Problem List", "Allergies and Intolerances", "Medications", "Results"} <= set(sections)
    res = sections["Results"]
    val = res.find(".//v3:entry/v3:observation/v3:value", NS)
    assert val.get("value") == "10.1" and val.get("unit") == "g/dL"
    assert res.find(".//v3:interpretationCode", NS).get("code") == "L"
    assert "Hemoglobin" in etree.tostring(res.find("v3:text", NS)).decode()

    # Re-importing our own CCD matches the same person and de-duplicates.
    before = client.get(f"{base}/chart", headers=doc).json()["sections"]
    imp = client.post("/api/interop/cda", headers={**doc, "Content-Type": "application/xml"}, content=r.content).json()
    assert imp["person_id"] == pid and imp["outcome"] == "matched_identifier"
    after = client.get(f"{base}/chart", headers=doc).json()["sections"]
    for sec in ("condition", "allergy", "medication", "observation"):
        assert len(after[sec]) == len(before[sec]), sec

    # An outside hospital's CCD creates the patient with records attributed to it.
    imp = client.post("/api/interop/cda", headers={**doc, "Content-Type": "application/xml"},
                      content=EXTERNAL_CCD).json()
    assert imp["imported"] == {"condition": 1, "observation": 1}
    ch = client.get(f"/api/clinical/patients/{imp['person_id']}/chart", headers=doc).json()
    cond = ch["sections"]["condition"][0]
    assert cond["display"] == "Chronic kidney disease" and cond["source"]["facility_oid"] == "2.25.7777"
    assert ch["sections"]["observation"][0]["value_num"] == 2.4
    assert ch["sections"]["document"][0]["doc_type"] == "cda"
    bad = client.post("/api/interop/cda", headers={**doc, "Content-Type": "application/xml"}, content=b"<x/>")
    assert bad.status_code == 422


def test_ems_prearrival_board_and_handover(client, users, monkeypatch):
    monkeypatch.setenv("EMS_ALERT_EMAIL", "ed-charge@example.org")
    doc = users["doctor"]
    r = client.post("/api/ems/notify", headers={**doc, "Content-Type": "application/xml"}, content=NEMSIS_XML)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["alerts"][0]["dry_run"] is True and "EMS INBOUND (critical)" in out["alerts"][0]["body"]
    board = client.get("/api/ems/board", headers=doc).json()["notifications"]
    n = board[0]
    assert n["status"] == "inbound" and n["unit"] == "MEDIC-7" and n["triage"] == "critical"
    assert n["chief_complaint"] == "Chest pain"
    assert n["data"]["latest_vitals"]["sbp"] == 88 and n["data"]["latest_vitals"]["hr"] == 118
    assert "BP 88/54" in n["summary"] and "HR 118" in n["summary"] and "Aspirin 300 mg" in n["summary"]
    assert n["patient"]["name"] == "Kamran Navabi"
    pid = out["person_id"]
    chart = client.get(f"/api/clinical/patients/{pid}/chart", headers=doc).json()
    enc = chart["sections"]["encounter"][0]
    assert enc["class"] == "EMER" and enc["status"] == "planned" and enc["admit_source"] == "EMS"
    vit = {o["display"]: o["value_num"] for o in chart["sections"]["observation"]}
    assert vit["Systolic blood pressure"] == 88 and vit["Oxygen saturation"] == 91
    assert {m["display"] for m in chart["sections"]["medication"]} == {"Aspirin 300 mg", "Nitroglycerin SL"}
    assert chart["sections"]["document"][0]["doc_type"] == "ems-report"
    assert any(i["system"] == "urn:aranmed:national-id" and i["value"] == "5566778899"
               for i in chart["person"]["identifiers"])

    # Updated report for the same incident updates in place.
    client.post("/api/ems/notify", headers={**doc, "Content-Type": "application/xml"},
                content=NEMSIS_XML.replace(b"<eVitals.10>118", b"<eVitals.10>124"))
    board = client.get("/api/ems/board", headers=doc).json()["notifications"]
    assert len(board) == 1 and "HR 124" in board[0]["summary"]

    # ED workflow: acknowledge -> arrived -> handed over; encounter follows.
    for st, enc_st in (("acknowledged", "planned"), ("arrived", "arrived"), ("handed_over", "in-progress")):
        r = client.post(f"/api/ems/{n['id']}/{st}", headers=doc)
        assert r.json()["status"] == st
        ch = client.get(f"/api/clinical/patients/{pid}/chart", headers=doc).json()
        assert ch["sections"]["encounter"][0]["status"] == enc_st
    assert client.post(f"/api/ems/{n['id']}/teleport", headers=doc).status_code == 400

    # JSON (NEMSIS-keyed) and FHIR Bundle variants.
    js = {"eResponse.03": "INC-J1", "eResponse.14": "MEDIC-9", "ePatient.02": "Unknown", "ePatient.13": "9906001",
          "eSituation.04": "Fall", "vitals": [{"eVitals.10": 88, "eVitals.12": 97}]}
    r = client.post("/api/ems/notify", headers=doc, json=js)
    assert r.status_code == 200
    fb = {"resourceType": "Bundle", "type": "collection", "id": "fhir-ems-1", "entry": [
        {"resource": {"resourceType": "Patient", "name": [{"family": "Saberi", "given": ["Mina"]}],
                      "gender": "female", "birthDate": "2001-01-01"}},
        {"resource": {"resourceType": "Encounter", "id": "INC-F1", "status": "planned",
                      "class": {"code": "EMER"}, "reasonCode": [{"text": "Seizure"}]}},
        {"resource": {"resourceType": "Observation", "status": "final",
                      "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
                      "valueQuantity": {"value": 132}}}]}
    r = client.post("/api/ems/notify", headers=doc, json=fb)
    assert r.status_code == 200
    complaints = {x["chief_complaint"] for x in client.get("/api/ems/board", headers=doc).json()["notifications"]}
    assert complaints == {"Chest pain", "Fall", "Seizure"}
    assert client.post("/api/ems/notify", headers=users["student"], json=js).status_code == 403


def test_ems_agency_with_peer_token(client, users, monkeypatch):
    """An EMS agency system (registered facility, kind ems) posts with a signed token."""
    from clinicaldb import principal
    monkeypatch.setenv("EMS_AGENCY_SECRET", "ems-shared-secret")
    client.post("/api/facilities", headers=users["admin"], json={
        "oid": "2.25.5050", "name": "City EMS", "kind": "ems", "trust_level": "ems",
        "secret_env": "EMS_AGENCY_SECRET"})
    monkeypatch.setenv("FACILITY_OID", "2.25.5050")  # mint as the agency
    token = principal.mint_peer_token(audience_oid="2.25.1001", secret="ems-shared-secret",
                                      practitioner="medic-42", purpose="ETREAT")
    monkeypatch.setenv("FACILITY_OID", "2.25.1001")
    r = client.post("/api/ems/notify", headers={"Authorization": f"Bearer {token}",
                                                "Content-Type": "application/xml"}, content=NEMSIS_XML)
    assert r.status_code == 200, r.text
    n = client.get("/api/ems/board", headers=users["doctor"]).json()["notifications"][0]
    assert n["source_facility"] == "2.25.5050"
    # An EMS agency may notify but not read the PACS.
    assert client.get("/api/dicom-web/studies", headers={"Authorization": f"Bearer {token}"}).status_code == 403
    bad = principal.mint_peer_token(audience_oid="2.25.1001", secret="wrong", purpose="ETREAT")
    assert client.post("/api/ems/notify", headers={"Authorization": f"Bearer {bad}"},
                       content=NEMSIS_XML).status_code == 401
