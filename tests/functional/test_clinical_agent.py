"""Functional: the agent's clinical (EHR / interop) tools, executed for real."""
from __future__ import annotations

import asyncio

from tests.functional.test_cda_ems import NEMSIS_XML


def _ctx(username: str):
    import auth
    from registry import Registry
    from tools.base import ToolContext
    u = auth.authenticate(username=username, password="functional-pass-2026")
    return ToolContext(registry=Registry.get(), owner_user_id=u["id"])


def test_agent_answers_from_chart_and_ems_board(client, users, fake_core):
    doc = users["doctor"]
    pid = client.post("/api/clinical/patients", headers=doc, json={
        "demographics": {"family": "Navabi", "given": "Kamran", "birth_date": "1961-07-09", "sex": "male"},
        "national_id": "5566778899", "mrn": "AG-CL-1"}).json()["person_id"]
    client.post(f"/api/clinical/patients/{pid}/allergies", headers=doc,
                json={"display": "Iodinated contrast", "reaction": "Anaphylaxis"})
    client.post(f"/api/clinical/patients/{pid}/medications", headers=doc,
                json={"display": "Clopidogrel", "dose": "75 mg", "status": "active"})
    # EMS brings the same man in (matched by national id).
    r = client.post("/api/ems/notify", headers={**doc, "Content-Type": "application/xml"}, content=NEMSIS_XML)
    assert r.json()["person_id"] == pid

    fake_core.script = [
        ("tool", "clinical_patient_summary", {"person_id": pid, "include_remote": False}),
        ("tool", "ems_inbound", {}),
        ("answer", "Inbound STEMI patient with iodinated contrast allergy; premedicate before cath."),
    ]
    r = client.post(f"/api/clinical/patients/{pid}/ask", headers=doc,
                    json={"question": "Anything the cath lab must know?", "include_remote": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["name"] for c in body["tool_calls"]] == ["clinical_patient_summary", "ems_inbound"]
    summary_out = body["tool_calls"][0]["result"]["content"]
    assert "Iodinated contrast" in summary_out and "Clopidogrel" in summary_out
    assert "BP 88/54" in body["tool_calls"][1]["result"]["content"]
    assert "contrast allergy" in body["answer"]


def test_clinical_tools_respect_permissions_and_restrictions(client, users):
    from tools.base import registry as tools
    doc = users["doctor"]
    pid = client.post("/api/clinical/patients", headers=doc, json={
        "demographics": {"family": "Secret", "given": "Staff", "birth_date": "1980-01-01"},
        "mrn": "AG-CL-2"}).json()["person_id"]
    search = tools.get("clinical_patient_search")
    res = asyncio.run(search.run(_ctx("doctor_ft"), query="AG-CL-2"))
    assert pid in res.content
    assert asyncio.run(search.run(_ctx("student_ft"), query="x")).error  # no clinical.read

    client.post(f"/api/clinical/patients/{pid}/consents", headers=doc,
                json={"category": "restricted", "status": "active"})
    res = asyncio.run(tools.get("clinical_patient_summary").run(_ctx("doctor_ft"), person_id=pid))
    assert res.error and "break-the-glass" in res.error
    client.post(f"/api/clinical/patients/{pid}/break-glass", headers=doc,
                json={"reason": "Patient collapsed in clinic, need history"})
    res = asyncio.run(tools.get("clinical_patient_summary").run(_ctx("doctor_ft"), person_id=pid))
    assert not res.error and "Secret" in res.content

    res = asyncio.run(tools.get("request_patient_transfer").run(
        _ctx("doctor_ft"), person_id=pid, to_facility="2.25.404", reason="x"))
    assert res.error and "not an active peer" in res.error
    res = asyncio.run(tools.get("request_patient_transfer").run(
        _ctx("resident_ft"), person_id=pid, to_facility="2.25.404", reason="x"))
    assert res.error and "transfer.write" in res.error
    tl = asyncio.run(tools.get("clinical_timeline").run(_ctx("doctor_ft"), person_id=pid))
    assert not tl.error
    names = {s["name"] for s in tools.schemas()}
    assert {"clinical_patient_search", "clinical_patient_summary", "clinical_timeline",
            "request_patient_transfer", "transfer_status", "ems_inbound"} <= names
