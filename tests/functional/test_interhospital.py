"""Functional, multi-hospital: the patient journey the system exists for.

Two independent AranMed hospitals (separate processes, databases, PACS
storage, DICOM listeners) plus an EMS agency:

1. Hospital A has treated the patient: chart + imaging.
2. An ambulance brings the patient to Hospital B (EMS ePCR, pre-arrival).
3. B's doctor opens the chart: the patient is discovered at A by national id,
   and A's allergies, medications, labs, notes and imaging appear — tagged
   as coming from A. B pulls A's CT on demand. A's audit names B's doctor.
4. The patient refuses sharing with B at A: B no longer sees A's data —
   until B declares an emergency (ETREAT), which A audits as an override.
5. A transfers the patient to B: request -> accept -> package (FHIR record +
   imaging over STOW-RS) -> depart -> arrive -> complete, both sides in step.
   B now holds the record locally; re-sending the package adds no duplicates.
"""
from __future__ import annotations

import time

import pytest

from tests.functional.dicom_factory import make_study, to_bytes
from tests.functional.hospital import Hospital

SECRET_AB = "secret-between-A-and-B-0123456789"
SECRET_EMS = "secret-ems-to-B-0123456789"
NAT_ID = "0076543210"


@pytest.fixture(scope="module")
def net(tmp_path_factory):
    work = tmp_path_factory.mktemp("net")
    a = Hospital("Shiraz General (A)", "2.25.601", work, ae_title="HOSPA6",
                 extra_env={"PEER_B": SECRET_AB})
    b = Hospital("Tehran Heart Center (B)", "2.25.602", work, ae_title="HOSPB6",
                 extra_env={"PEER_A": SECRET_AB, "EMS_SECRET": SECRET_EMS})
    a.start()
    b.start()
    try:
        assert a.post("/api/facilities", json=b.facility_record(secret_env="PEER_B")).status_code == 200
        assert b.post("/api/facilities", json=a.facility_record(secret_env="PEER_A")).status_code == 200
        assert b.post("/api/facilities", json={"oid": "2.25.603", "name": "Province EMS", "kind": "ems",
                                               "trust_level": "ems", "secret_env": "EMS_SECRET"}).status_code == 200
        yield a, b
    finally:
        a.stop()
        b.stop()


def _stow(h: Hospital, dsets):
    from pacs import multipart
    bnd = multipart.boundary()
    body = multipart.encode(((to_bytes(d), "application/dicom") for d in dsets), bnd)
    r = h.post("/api/dicom-web/studies", content=body,
               headers={"Content-Type": multipart.content_type(bnd, "application/dicom")})
    assert r.status_code == 200, r.text


def _ems_token(b: Hospital) -> str:
    import os
    from clinicaldb import principal
    old = os.environ.get("FACILITY_OID")
    os.environ["FACILITY_OID"] = "2.25.603"
    try:
        return principal.mint_peer_token(audience_oid=b.oid, secret=SECRET_EMS,
                                         practitioner="medic-7", purpose="ETREAT")
    finally:
        if old is None:
            os.environ.pop("FACILITY_OID")
        else:
            os.environ["FACILITY_OID"] = old


def test_patient_journey_across_hospitals(net):
    a, b = net
    # ---- 1. History at Hospital A ---------------------------------------------
    reg = a.post("/api/clinical/patients", role="doctor", json={
        "demographics": {"family": "Farahani", "given": "Reza", "birth_date": "1958-04-11", "sex": "male"},
        "mrn": "A-7788", "national_id": NAT_ID}).json()
    pa = reg["person_id"]
    base_a = f"/api/clinical/patients/{pa}"
    for kind, body in (
        ("allergies", {"display": "Iodinated contrast", "reaction": "Anaphylaxis", "criticality": "high"}),
        ("conditions", {"display": "Coronary artery disease", "clinical_status": "active"}),
        ("medications", {"display": "Clopidogrel", "dose": "75 mg", "frequency": "daily", "status": "active"}),
        ("observations", {"category": "laboratory", "code_system": "http://loinc.org", "code": "2160-0",
                          "display": "Creatinine", "value_num": 1.8, "unit": "mg/dL", "ref_low": 0.6,
                          "ref_high": 1.2, "effective": "2026-08-01"}),
        ("documents", {"doc_type": "discharge-summary", "title": "Discharge after PCI",
                       "content": "PCI to LAD in 2024. On DAPT.", "effective": "2024-05-02"}),
    ):
        assert a.post(f"{base_a}/{kind}", role="doctor", json=body).status_code == 200, kind
    ct_uid, ct = make_study(n_series=1, per_series=3, patient_name="Farahani^Reza", patient_id="A-7788",
                            issuer="2.25.601", birth_date="19580411", study_desc="CT CORONARY ANGIO",
                            study_date="20260801")
    _stow(a, ct)

    # ---- 2. EMS brings the patient to Hospital B ------------------------------------
    ems_xml = f"""<EMSDataSet xmlns="http://www.nemsis.org"><Header><PatientCareReport>
      <eResponse><eResponse.03>INC-77</eResponse.03><eResponse.14>AMB-3</eResponse.14></eResponse>
      <ePatient><ePatient.02>Farahani</ePatient.02><ePatient.03>Reza</ePatient.03>
        <ePatient.12>{NAT_ID}</ePatient.12><ePatient.13>9906003</ePatient.13><ePatient.17>1958-04-11</ePatient.17></ePatient>
      <eSituation><eSituation.04>Chest pain, diaphoresis</eSituation.04><eSituation.13>2813001</eSituation.13></eSituation>
      <eVitals><eVitals.VitalGroup><eVitals.06>92</eVitals.06><eVitals.07>60</eVitals.07><eVitals.10>110</eVitals.10></eVitals.VitalGroup></eVitals>
    </PatientCareReport></Header></EMSDataSet>"""
    import httpx
    r = httpx.post(f"{b.base}/api/ems/notify", content=ems_xml.encode(), timeout=30,
                   headers={"Authorization": f"Bearer {_ems_token(b)}", "Content-Type": "application/xml"})
    assert r.status_code == 200, r.text
    pb = r.json()["person_id"]
    board = b.get("/api/ems/board", role="doctor").json()["notifications"]
    assert board[0]["chief_complaint"] == "Chest pain, diaphoresis" and board[0]["source_facility"] == "2.25.603"

    # ---- 3. B opens the chart with records from other hospitals ------------------------
    chart = b.get(f"/api/clinical/patients/{pb}/chart", role="doctor",
                  params={"include_remote": "true"}).json()
    assert chart["errors"] == [], chart["errors"]
    assert "2.25.601" in chart["facilities"]
    allergies = chart["sections"]["allergy"]
    remote_allergy = next(x for x in allergies if x["display"] == "Iodinated contrast")
    assert remote_allergy["source"]["held"] == "remote" and remote_allergy["source"]["facility_oid"] == "2.25.601"
    assert remote_allergy["source"]["facility"] == "Shiraz General (A)"
    assert {m["display"] for m in chart["sections"]["medication"]} >= {"Clopidogrel"}
    labs = [o for o in chart["sections"]["observation"] if o.get("display") == "Creatinine"]
    assert labs and labs[0]["value_num"] == 1.8 and labs[0]["interpretation"] == "H"
    assert any(d.get("title") == "Discharge after PCI" for d in chart["sections"]["document"])
    # B's own EMS data sits beside it, held locally.
    assert any(e["source"]["held"] == "local" and e.get("class") == "EMER" for e in chart["sections"]["encounter"])
    remote_ct = next(s for s in chart["imaging"] if s["StudyInstanceUID"] == ct_uid)
    assert remote_ct["locations"][0]["id"] == "facility:2.25.601"

    # Pull the CT into B's PACS on demand.
    job = b.post("/api/pacs/retrieve", role="doctor", json={"study_uid": ct_uid, "node_id": "facility:2.25.601",
                                                            "wait": True}).json()
    assert job["status"] == "done" and job["result"]["stored"] == 3
    local = b.get(f"/api/clinical/patients/{pb}/chart", role="doctor").json()
    assert ct_uid in [s["StudyInstanceUID"] for s in local["imaging"]]  # linked to B's person via the MPI

    # A's audit trail names B's doctor and the purpose.
    audit_a = a.get("/api/audit", params={"limit": 300}).json()["entries"]
    peer_reads = [e for e in audit_a if e["action"].startswith("fhir.") and (e["actor_name"] or "").startswith("peer:2.25.602")]
    assert peer_reads and any("doctor_hospb6" in e["actor_name"] for e in peer_reads)
    assert all((e["detail"] or {}).get("purpose") == "TREAT" for e in peer_reads)

    # ---- 4. Consent: the patient refuses sharing with B ---------------------------
    assert a.post(f"{base_a}/consents", role="doctor", json={
        "category": "deny-sharing", "grantee": "2.25.602", "status": "active", "scope": "patient-privacy",
        "text": "Patient declines sharing with Tehran Heart Center"}).status_code == 200
    chart = b.get(f"/api/clinical/patients/{pb}/chart", role="doctor", params={"include_remote": "true"}).json()
    assert not any(x["display"] == "Iodinated contrast" for x in chart["sections"]["allergy"])
    assert chart["errors"], "the refusal should surface as an error, not silently"
    # Emergency treatment overrides the refusal, and A records it as such.
    chart = b.get(f"/api/clinical/patients/{pb}/chart", role="doctor",
                  params={"include_remote": "true", "purpose": "ETREAT"}).json()
    assert any(x["display"] == "Iodinated contrast" for x in chart["sections"]["allergy"])
    audit_a = a.get("/api/audit", params={"limit": 400}).json()["entries"]
    assert any((e["detail"] or {}).get("why") == "emergency override" for e in audit_a)
    assert any(e["outcome"] == "deny" and (e["detail"] or {}).get("why", "").startswith("patient has refused")
               for e in audit_a)
    # Residents at B may not declare an emergency.
    assert b.get(f"/api/clinical/patients/{pb}/chart", role="resident",
                 params={"include_remote": "true", "purpose": "ETREAT"}).status_code == 403
    # The patient withdraws the refusal for the transfer that follows.
    consent = a.get(f"{base_a}/consents", role="doctor").json()["items"][0]
    import httpx as _h
    r = _h.patch(f"{a.base}/api/clinical/resources/consent/{consent['id']}", headers=a.h("doctor"),
                 json={"changes": {"status": "inactive"}}, timeout=30)
    assert r.status_code == 200

    # ---- 5. Transfer A -> B -------------------------------------------------------------
    mr_uid, mr = make_study(n_series=1, per_series=2, modality="MR", patient_name="Farahani^Reza",
                            patient_id="A-7788", issuer="2.25.601", study_desc="CARDIAC MRI",
                            study_date="20260901")
    _stow(a, mr)
    t = a.post("/api/transfers", role="doctor", json={
        "person_id": pa, "to_facility": "2.25.602", "urgency": "urgent",
        "reason": "Primary PCI capability", "clinical_summary": "NSTEMI, hemodynamically borderline",
        "transport_mode": "ALS ambulance", "include_imaging": True}).json()
    assert t["status"] == "requested" and t["remote_id"], t
    incoming = b.get("/api/transfers", role="doctor", params={"direction": "incoming"}).json()["transfers"]
    tb = next(x for x in incoming if x["remote_id"] == t["id"])
    assert tb["status"] == "requested" and tb["from_facility"] == "2.25.601"
    assert tb["person_id"] == pb  # the incoming patient matched B's existing person (national id)
    # Only the receiving side may accept.
    assert a.post(f"/api/transfers/{t['id']}/accept", role="doctor", json={}).status_code == 409
    r = b.post(f"/api/transfers/{tb['id']}/accept", role="doctor", json={"eta": "2026-10-03T23:30:00", "note": "Cath lab ready"})
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    assert a.get(f"/api/transfers/{t['id']}", role="doctor").json()["status"] == "accepted"
    # The package is sent automatically on acceptance.
    deadline = time.time() + 60
    while time.time() < deadline:
        tb_now = b.get(f"/api/transfers/{tb['id']}", role="doctor").json()
        if tb_now["package_status"] == "received":
            break
        time.sleep(0.5)
    assert tb_now["package_status"] == "received", tb_now
    imported = tb_now["package_manifest"]["imported"]
    assert imported.get("AllergyIntolerance") == 1 and imported.get("MedicationStatement") == 1
    deadline = time.time() + 60
    while time.time() < deadline:
        ta = a.get(f"/api/transfers/{t['id']}", role="doctor").json()
        if ta["package_status"] in ("delivered", "partial", "failed"):
            break
        time.sleep(0.5)
    assert ta["package_status"] == "delivered", ta
    assert {s["study_uid"] for s in ta["package_manifest"]["studies"]} == {ct_uid, mr_uid}

    # B now holds A's record locally (no live federation needed), attributed to A.
    local = b.get(f"/api/clinical/patients/{pb}/chart", role="doctor").json()
    allergy = next(x for x in local["sections"]["allergy"] if x["display"] == "Iodinated contrast")
    assert allergy["source"]["held"] == "local" and allergy["source"]["facility_oid"] == "2.25.601"
    assert any(d["doc_type"] == "transfer-summary" for d in local["sections"]["document"])
    assert {mr_uid, ct_uid} <= {s["StudyInstanceUID"] for s in local["imaging"]}
    counts = {k: len(v) for k, v in local["sections"].items()}

    # Movement: A departs, B records arrival and completion; both sides agree.
    assert a.post(f"/api/transfers/{t['id']}/depart", role="doctor", json={"note": "AMB-3 en route"}).status_code == 200
    assert b.get(f"/api/transfers/{tb['id']}", role="doctor").json()["status"] == "in_transit"
    assert b.post(f"/api/transfers/{tb['id']}/arrive", role="doctor", json={}).status_code == 200
    assert b.post(f"/api/transfers/{tb['id']}/complete", role="doctor", json={}).status_code == 200
    ta = a.get(f"/api/transfers/{t['id']}", role="doctor").json()
    assert ta["status"] == "completed"
    assert [h["status"] for h in ta["history"] if h.get("status")] == \
        ["requested", "accepted", "in_transit", "arrived", "completed"]
    assert b.post(f"/api/transfers/{tb['id']}/arrive", role="doctor", json={}).status_code == 409

    # Once completed, B refuses further packages for this transfer and nothing
    # changes (re-sending while still open is covered in test_interhospital_records).
    r = a.post(f"/api/transfers/{t['id']}/resend-package", role="doctor")
    assert r.status_code == 502 and "not expected in state completed" in r.text
    again = b.get(f"/api/clinical/patients/{pb}/chart", role="doctor").json()
    assert {k: len(v) for k, v in again["sections"].items()} == counts

    # Both message logs recorded the exchange.
    assert any(m["protocol"] == "transfer" for m in a.get("/api/interop/messages").json()["messages"])
    assert any(m["protocol"] == "ems" for m in b.get("/api/interop/messages").json()["messages"])


def test_peer_test_endpoint_reports_compatibility(net):
    a, b = net
    out = a.post("/api/interop/peers/test", json={"oid": b.oid}).json()
    assert out["reachable"] and out["schema_compatible"] and out["oid_matches"]
    assert out["fhir"] and out["token_accepted"]
