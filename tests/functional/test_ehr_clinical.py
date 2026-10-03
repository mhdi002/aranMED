"""Functional: the relational EHR, its links to the PACS, access control and
the legacy-record bridge — all through the HTTP API."""
from __future__ import annotations

import base64
import json
import os

import db
from tests.functional.dicom_factory import make_study, to_bytes


def _register(client, h, **kw):
    r = client.post("/api/clinical/patients", headers=h, json=kw)
    assert r.status_code == 200, r.text
    return r.json()


def test_chart_lifecycle_with_imaging_orders_and_reports(client, users, monkeypatch):
    import phi_crypto
    monkeypatch.setenv("PHI_ENCRYPTION_KEYS", "k1:" + base64.b64encode(os.urandom(32)).decode())
    phi_crypto.reload_keys()
    doc, rad = users["doctor"], users["radiologist"]
    reg = _register(client, doc, demographics={"family": "Mohammadi", "given": "Zahra",
                                               "birth_date": "1985-03-20", "sex": "female"},
                    mrn="TGH-500", national_id="0099887766")
    pid = reg["person_id"]
    base = f"/api/clinical/patients/{pid}"

    # --- record clinical data ---------------------------------------------
    enc = client.post(f"{base}/encounters", headers=doc, json={
        "class": "EMER", "status": "in-progress", "start_at": "2026-10-01T08:30:00",
        "reason": "Chest pain", "department": "Emergency"}).json()
    client.post(f"{base}/conditions", headers=doc, json={
        "display": "Type 2 diabetes mellitus", "code_system": "http://snomed.info/sct",
        "code": "44054006", "clinical_status": "active", "encounter_id": enc["id"]})
    client.post(f"{base}/allergies", headers=doc, json={
        "display": "Penicillin", "reaction": "Anaphylaxis", "criticality": "high",
        "status": "active"})
    client.post(f"{base}/medications", headers=doc, json={
        "display": "Metformin", "dose": "500 mg", "route": "oral", "frequency": "BID",
        "status": "active"})
    hb = client.post(f"{base}/observations", headers=doc, json={
        "category": "laboratory", "code_system": "http://loinc.org", "code": "4548-4",
        "display": "HbA1c", "value_num": 8.4, "unit": "%", "ref_low": 4.0, "ref_high": 5.6,
        "effective": "2026-10-01", "status": "final"}).json()
    client.post(f"{base}/observations", headers=doc, json={
        "category": "vital-signs", "code_system": "http://loinc.org", "code": "8867-4",
        "display": "Heart rate", "value_num": 104, "unit": "/min",
        "effective": "2026-10-01T08:35:00"})
    client.post(f"{base}/procedures", headers=doc, json={"display": "ECG", "performed": "2026-10-01"})
    client.post(f"{base}/immunizations", headers=doc, json={"display": "Influenza vaccine",
                                                             "occurrence": "2025-11-01"})
    note = client.post(f"{base}/documents", headers=doc, json={
        "doc_type": "discharge-summary", "title": "ED summary", "content_type": "text/plain",
        "content": "Patient with atypical chest pain; troponin negative.",
        "effective": "2026-10-01"}).json()
    assert "content" not in note

    # Abnormal value flagged from its reference range.
    labs = client.get(f"{base}/observations", headers=doc).json()["items"]
    assert next(o for o in labs if o["id"] == hb["id"])["interpretation"] == "H"

    # Document content is encrypted at rest and decrypted for authorised reads.
    with db.connect() as c:
        raw = c.execute("SELECT content FROM ehr_documents WHERE id=?", (note["id"],)).fetchone()["content"]
    assert raw.startswith("ARANMED-PHI-v1:") and "troponin" not in raw
    assert "troponin negative" in client.get(f"/api/clinical/documents/{note['id']}",
                                             headers=doc).json()["content"]

    # --- imaging: a study for the same MRN joins the chart --------------------
    from pacs import index
    study_uid, dsets = make_study(n_series=1, per_series=2, patient_name="Mohammadi^Zahra",
                                  patient_id="TGH-500", birth_date="19850320", sex="F",
                                  study_desc="CT CHEST", study_date="20261001")
    for d in dsets:
        index.ingest(to_bytes(d), source="test")
    chart = client.get(f"{base}/chart", headers=doc).json()
    assert [s["StudyInstanceUID"] for s in chart["imaging"]] == [study_uid]
    sm = chart["summary"]
    assert [c["display"] for c in sm["active_problems"]] == ["Type 2 diabetes mellitus"]
    assert sm["abnormal_labs"][0]["display"] == "HbA1c"
    assert sm["latest_vitals"][0]["value_num"] == 104
    assert chart["sections"]["allergy"][0]["source"]["facility_oid"] == "2.25.1001"
    assert all("content" not in d for d in chart["sections"]["document"])

    # --- imaging order -> worklist; PACS final report -> DiagnosticReport -------
    order = client.post(f"{base}/service-requests", headers=doc, json={
        "category": "imaging", "status": "active", "intent": "order", "priority": "stat",
        "display": "CT Pulmonary Angiography", "code": "CTPA", "reason": "Rule out PE",
        "data": {"modality": "CT"}}).json()
    order = client.get(f"{base}/service-requests", headers=doc).json()["items"][0]
    assert order["accession"]
    wl = client.get("/api/pacs/worklist", headers=doc).json()["items"]
    item = next(w for w in wl if w["accession"] == order["accession"])
    assert item["person_id"] == pid and item["priority"] == "STAT"
    assert item["patient_id"] == "TGH-500" and item["order_id"] == order["id"]

    r = client.post(f"/api/pacs/studies/{study_uid}/reports", headers=rad, json={
        "text": "CT CHEST\nFINDINGS: No PE.\nIMPRESSION: No pulmonary embolism.",
        "status": "final"})
    assert r.status_code == 200
    reports = client.get(f"{base}/diagnostic-reports", headers=doc).json()["items"]
    assert reports[0]["study_uid"] == study_uid
    assert reports[0]["status"] == "final"
    assert reports[0]["conclusion"] == "No pulmonary embolism."

    # --- versioning ------------------------------------------------------------
    med = client.get(f"{base}/medications", headers=doc).json()["items"][0]
    ok = client.patch(f"/api/clinical/resources/medication/{med['id']}", headers=doc,
                      json={"changes": {"dose": "1000 mg"}, "version": med["version"]})
    assert ok.json()["version"] == med["version"] + 1
    stale = client.patch(f"/api/clinical/resources/medication/{med['id']}", headers=doc,
                         json={"changes": {"dose": "250 mg"}, "version": med["version"]})
    assert stale.status_code == 409
    hist = client.get(f"/api/clinical/resources/medication/{med['id']}/history",
                      headers=doc).json()["versions"]
    assert [v["dose"] for v in hist] == ["500 mg", "1000 mg"]

    # --- timeline ----------------------------------------------------------------
    events = client.get(f"{base}/timeline", headers=doc).json()["events"]
    kinds = {e["kind"] for e in events}
    assert {"encounter", "observation", "imaging", "diagnostic_report", "document"} <= kinds
    assert events == sorted(events, key=lambda e: e["date"], reverse=True)
    monkeypatch.delenv("PHI_ENCRYPTION_KEYS")
    phi_crypto.reload_keys()


def test_restricted_record_needs_break_glass(client, users):
    doc, res = users["doctor"], users["resident"]
    pid = _register(client, doc, demographics={"family": "Vip", "given": "Person",
                                               "birth_date": "1970-01-01"}, mrn="VIP-1")["person_id"]
    base = f"/api/clinical/patients/{pid}"
    assert client.get(f"{base}/chart", headers=res).status_code == 200
    client.post(f"{base}/consents", headers=doc, json={
        "category": "restricted", "status": "active", "scope": "patient-privacy",
        "text": "Staff member record"})
    r = client.get(f"{base}/chart", headers=res)
    assert r.status_code == 403 and "break-the-glass" in r.json()["detail"]
    assert client.get(f"{base}/chart", headers=doc).status_code == 403
    # Residents may not break the glass; doctors must give a real reason.
    assert client.post(f"{base}/break-glass", headers=res,
                       json={"reason": "emergency in ED now"}).status_code == 403
    assert client.post(f"{base}/break-glass", headers=doc, json={"reason": "x"}).status_code == 400
    g = client.post(f"{base}/break-glass", headers=doc,
                    json={"reason": "Unconscious patient in resus, need allergy history"})
    assert g.status_code == 200
    ch = client.get(f"{base}/chart", headers=doc).json()
    assert ch["access"]["basis"] == "break-glass" and ch["access"]["restricted"] is True
    assert client.get(f"{base}/chart", headers=res).status_code == 403  # grant is personal
    import audit
    rows = audit.query(limit=200)
    assert any(r["action"] == "clinical.breakglass" for r in rows)
    assert any(r["action"] == "clinical.chart.read" and r["outcome"] == "deny" for r in rows)


def test_legacy_ehr_record_flows_into_relational_chart(client, users, fake_core):
    doc = users["doctor"]
    legacy = {"patient": {"name": "Ali Reza", "age": 62, "sex": "male", "mrn": "LEG-62"},
              "encounter": {"date": "2026-09-30", "chief_complaint": "Cough and dyspnea"},
              "problems": [{"name": "COPD", "status": "active"}],
              "allergies": [{"substance": "Sulfa", "reaction": "rash"}],
              "medications": [{"name": "Aspirin", "dose": "81 mg", "frequency": "daily",
                               "frequency_hours": 24},
                              {"name": "Salbutamol", "dose": "100 mcg", "route": "inhaled"}],
              "vitals": {"bp": "130/85", "hr": 92, "spo2": 93},
              "notes": "Smoker, 40 pack-years."}
    fake_core.script = [("answer", json.dumps(legacy))]
    r = client.post("/api/ehr/build", headers=doc,
                    json={"patient_info": "62yo male COPD", "language": "en"})
    assert r.status_code == 200, r.text
    legacy_id = r.json()["patient_id"]

    hits = client.get("/api/clinical/patients", headers=doc, params={"identifier": "LEG-62"}).json()
    assert len(hits["results"]) == 1
    pid = hits["results"][0]["person_id"]
    chart = client.get(f"/api/clinical/patients/{pid}/chart", headers=doc).json()
    meds = sorted(m["display"] for m in chart["sections"]["medication"])
    assert meds == ["Aspirin", "Salbutamol"]
    assert chart["sections"]["condition"][0]["display"] == "COPD"
    assert chart["sections"]["allergy"][0]["reaction"] == "rash"
    assert {o["display"] for o in chart["summary"]["latest_vitals"]} >= {"Heart rate", "Oxygen saturation"}
    assert chart["sections"]["encounter"][0]["reason"] == "Cough and dyspnea"
    assert chart["sections"]["document"][0]["title"] == "Clinical note"

    # Editing the legacy record (dropping Salbutamol) is mirrored, not appended.
    legacy["medications"] = legacy["medications"][:1]
    import store as legacy_store, auth
    owner = auth.authenticate(username="doctor_ft", password="functional-pass-2026")
    legacy_store.upsert_patient(owner_user_id=owner["id"], data=legacy, patient_id=legacy_id)
    chart = client.get(f"/api/clinical/patients/{pid}/chart", headers=doc).json()
    assert [m["display"] for m in chart["sections"]["medication"]] == ["Aspirin"]


def test_chart_ask_uses_agent_with_chart_context(client, users, fake_core):
    doc = users["doctor"]
    pid = _register(client, doc, demographics={"family": "Ask", "given": "Me",
                                               "birth_date": "1990-01-01"}, mrn="ASK-1")["person_id"]
    client.post(f"/api/clinical/patients/{pid}/allergies", headers=doc,
                json={"display": "Latex", "reaction": "urticaria"})
    fake_core.script = [("answer", "The patient is allergic to latex.")]
    r = client.post(f"/api/clinical/patients/{pid}/ask", headers=doc,
                    json={"question": "Any allergies?", "include_remote": False})
    assert r.status_code == 200, r.text
    assert "latex" in r.json()["answer"]
    prompt = [m for m in fake_core.received[0] if m.role == "user"][-1].content
    assert "Latex" in prompt and "Allergies:" in prompt


def test_merge_keeps_encrypted_documents_readable(client, users, monkeypatch):
    import phi_crypto
    monkeypatch.setenv("PHI_ENCRYPTION_KEYS", "k1:" + base64.b64encode(os.urandom(32)).decode())
    phi_crypto.reload_keys()
    doc = users["doctor"]
    a = _register(client, doc, demographics={"family": "Dup", "given": "One",
                                             "birth_date": "1960-01-01"}, mrn="D-1")["person_id"]
    b = _register(client, doc, demographics={"family": "Other", "given": "Two",
                                             "birth_date": "1961-02-02"}, mrn="D-2")["person_id"]
    d = client.post(f"/api/clinical/patients/{b}/documents", headers=doc, json={
        "title": "Old note", "content": "Secret history on B", "doc_type": "progress-note"}).json()
    assert client.post("/api/mpi/merge", headers=doc,
                       json={"survivor_id": a, "merged_id": b}).json()["changed"]
    got = client.get(f"/api/clinical/documents/{d['id']}", headers=doc).json()
    assert got["person_id"] == a and got["content"] == "Secret history on B"
    assert client.get(f"/api/clinical/patients/{b}/chart", headers=doc).json()["person"]["id"] == a
    monkeypatch.delenv("PHI_ENCRYPTION_KEYS")
    phi_crypto.reload_keys()
