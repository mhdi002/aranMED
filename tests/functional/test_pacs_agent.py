"""Functional: the agent working with PACS data end to end.

The core LLM and vision model are scripted fakes (deterministic), but every
tool really executes: studies are queried from the real index, images are
really rendered from stored DICOM (and asserted to reach the vision model as
PNGs), reports are really saved and linked, permissions and audit are real.
"""
from __future__ import annotations

import io

from PIL import Image

from tests.functional.dicom_factory import make_study, to_bytes


def _ingest_study(**kw):
    from pacs import index
    study_uid, dsets = make_study(**kw)
    for d in dsets:
        index.ingest(to_bytes(d), source="test")
    return study_uid


def test_agent_finds_and_reads_study_via_ask(client, users, fake_core, fake_vision):
    study_uid = _ingest_study(n_series=2, per_series=5, patient_name="Rahimi^Neda",
                              patient_id="AG-1", accession="AG-ACC", study_desc="CT CHEST",
                              body_part="CHEST")
    seen_images: list[bytes] = []
    real_chat = fake_vision.chat

    async def spy(messages, **kw):
        for m in messages:
            seen_images.extend(m.images or [])
        return await real_chat(messages, **kw)
    fake_vision.chat = spy

    fake_core.script = [
        ("tool", "pacs_search_studies", {"patient_name": "rahimi", "modality": "CT"}),
        ("tool", "pacs_analyze_study", {"study_uid": study_uid, "question": "rule out pneumonia"}),
        # consumed by structure_report inside pacs_analyze_study
        ("answer", "CT CHEST\nFINDINGS: Bilateral perihilar opacities.\nIMPRESSION: Possible pneumonia."),
        ("answer", "I found the CT chest and saved a draft report for radiologist review."),
    ]
    r = client.post(f"/api/pacs/studies/{study_uid}/ask", headers=users["doctor"],
                    json={"question": "Find Neda Rahimi's chest CT and draft a report."})
    assert r.status_code == 200, r.text
    body = r.json()
    names = [c["name"] for c in body["tool_calls"]]
    assert names == ["pacs_search_studies", "pacs_analyze_study"]
    assert "draft report" in body["answer"]

    # The search tool actually found the study.
    search_call = body["tool_calls"][0]
    assert study_uid in search_call["result"]["content"]

    # The vision model received real rendered key images (one per series).
    assert len(seen_images) == 2
    for png in seen_images:
        img = Image.open(io.BytesIO(png))
        assert img.format == "PNG" and img.size == (64, 64)

    # A DRAFT report authored by the agent is now linked to the study.
    detail = client.get(f"/api/pacs/studies/{study_uid}", headers=users["doctor"]).json()
    reps = detail["reports"]
    assert len(reps) == 1
    assert reps[0]["status"] == "draft" and reps[0]["source"] == "agent"
    assert reps[0]["author"] == "agent:doctor_ft"
    assert "IMPRESSION" in reps[0]["text"]
    assert reps[0]["data"]["findings"].startswith("Bilateral perihilar")
    assert detail["study"]["_ext"]["status"] == "received"  # drafts don't change status

    # The core LLM saw the study context the endpoint injected.
    first_user = [m for m in fake_core.received[0] if m.role == "user"][-1].content
    assert study_uid in first_user

    import audit
    assert {"pacs.search", "pacs.agent.analyze"} <= {a["action"] for a in audit.query(limit=100)}


def test_analyze_endpoint_and_report_signoff(client, users, fake_core):
    study_uid = _ingest_study(n_series=1, per_series=3, modality="MR", study_desc="MRI BRAIN",
                              body_part="HEAD", patient_id="AG-2")
    fake_core.script = [("answer", "MRI BRAIN\nFINDINGS: No acute infarct.\nIMPRESSION: Normal.")]
    r = client.post(f"/api/pacs/studies/{study_uid}/analyze", headers=users["radiologist"],
                    json={"question": "headache"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["report_id"] and "IMPRESSION: Normal." in data["report"]
    assert len(data["images"]) == 1
    # Radiologist reviews and signs off the agent's draft.
    r = client.post(f"/api/pacs/studies/{study_uid}/reports", headers=users["radiologist"],
                    json={"text": data["report"] + "\nReviewed.", "status": "final",
                          "report_id": data["report_id"]})
    assert r.json()["status"] == "final"
    assert client.get(f"/api/pacs/studies/{study_uid}",
                      headers=users["doctor"]).json()["study"]["_ext"]["status"] == "reported"


def test_agent_tools_enforce_auth_and_rbac(client, users, fake_core):
    import asyncio
    from registry import Registry
    from tools.base import ToolContext, registry as tool_registry
    import auth

    study_uid = _ingest_study(n_series=1, per_series=1, patient_id="AG-3")
    # Students: endpoint refused.
    assert client.post(f"/api/pacs/studies/{study_uid}/analyze", headers=users["student"],
                       json={}).status_code == 403
    tool = tool_registry.get("pacs_search_studies")
    # Anonymous agent context (chat without a token): no imaging access.
    res = asyncio.run(tool.run(ToolContext(registry=Registry.get()), patient_id="AG-3"))
    assert res.error and "signed-in" in res.error
    # Student context: refused by RBAC (and audited).
    student = auth.authenticate(username="student_ft", password="functional-pass-2026")
    res = asyncio.run(tool.run(ToolContext(registry=Registry.get(),
                                           owner_user_id=student["id"]), patient_id="AG-3"))
    assert res.error and "pacs.read" in res.error
    # Resident: may read but not write reports.
    resident = auth.authenticate(username="resident_ft", password="functional-pass-2026")
    ctx = ToolContext(registry=Registry.get(), owner_user_id=resident["id"])
    assert not asyncio.run(tool.run(ctx, patient_id="AG-3")).error
    res = asyncio.run(tool_registry.get("pacs_link_report").run(ctx, study_uid=study_uid,
                                                                text="x"))
    assert res.error and "pacs.write" in res.error


def test_priors_and_worklist_tools(client, users):
    import asyncio
    import auth
    from registry import Registry
    from tools.base import ToolContext, registry as tool_registry
    from pacs import worklist

    old = _ingest_study(n_series=1, per_series=1, patient_id="AG-4", study_date="20250101",
                        accession="OLD-1")
    new = _ingest_study(n_series=1, per_series=1, patient_id="AG-4", study_date="20260901",
                        accession="NEW-1")
    doc = auth.authenticate(username="doctor_ft", password="functional-pass-2026")
    ctx = ToolContext(registry=Registry.get(), owner_user_id=doc["id"])
    from pacs import reports
    reports.save(old, text="IMPRESSION: 6 mm nodule RUL.", status="final")
    res = asyncio.run(tool_registry.get("pacs_compare_priors").run(ctx, study_uid=new))
    assert old in res.content and "6 mm nodule" in res.content
    assert [p["StudyInstanceUID"] for p in res.data["priors"]] == [old]

    worklist.create({"modality": "CT", "patient_name": "X^Y", "procedure_description": "CT HEAD",
                     "scheduled_start": "20261005T100000"})
    res = asyncio.run(tool_registry.get("pacs_worklist").run(ctx, modality="CT"))
    assert "CT HEAD" in res.content and len(res.data["items"]) == 1

    schemas = {s["name"] for s in tool_registry.schemas()}
    assert {"pacs_search_studies", "pacs_get_study", "pacs_analyze_study",
            "pacs_compare_priors", "pacs_link_report", "pacs_worklist"} <= schemas
