"""HTTP-level tests against the FastAPI app."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(fresh_registry):
    # `app` reads the singleton registry at import time, so re-import to bind
    # the *test* registry instance.
    import importlib
    import app as app_mod
    importlib.reload(app_mod)
    app_mod.registry = fresh_registry           # rebind
    from agent import Agent
    app_mod.agent = Agent(registry=fresh_registry, memory=app_mod.memory)
    with TestClient(app_mod.app) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"]
    assert j["core_model"] == "fake-core"


def test_models(client):
    r = client.get("/api/models")
    assert r.status_code == 200
    j = r.json()
    assert "fake-core" in j["providers"]
    assert "fake-asr" in j["providers"]
    assert "fake-vision" in j["providers"]


def test_templates(client):
    r = client.get("/api/templates"); r.raise_for_status()
    items = r.json()["templates"]
    assert items
    ids = {t["id"] for t in items}
    assert "thyroid" in ids or "chest" in ids
    tid = "thyroid" if "thyroid" in ids else items[0]["id"]
    body = client.get(f"/api/templates/{tid}").json()["body"]
    assert body.strip()


def test_transcribe_endpoint(client, wav_bytes):
    r = client.post("/api/transcribe",
                    files={"file": ("d.wav", wav_bytes, "audio/wav")})
    r.raise_for_status()
    j = r.json()
    assert "سگمان" in j["text"]
    assert j["asr_model"] == "fake-asr"


def test_report_endpoint(client, fake_core):
    fake_core.script = [("answer",
        "Thyroid Sonography:\nFINDINGS:\n Abnormality.\nIMPRESSION:\n 1. Lesion.")]
    r = client.post("/api/report", json={
        "transcript": "یک انورمالی در تیروئید دیده شد",
        "template_id": "thyroid",
    })
    r.raise_for_status()
    j = r.json()
    assert "Thyroid" in j["report"] or "FINDINGS" in j["report"].upper()
    assert j["model"] == "fake-core"


def test_dictate_endpoint(client, fake_core, wav_bytes):
    fake_core.script = [("answer",
        "Chest sonography:\nFINDINGS:\n Lesion.\nIMPRESSION:\n 1. Mass.")]
    r = client.post("/api/dictate",
                    files={"file": ("d.wav", wav_bytes, "audio/wav")},
                    data={"template_id": "chest"})
    r.raise_for_status()
    j = r.json()
    assert "IMPRESSION" in j["report"].upper() or "Chest" in j["report"]


def test_chat_endpoint_with_audio_and_image(client, fake_core, wav_bytes, png_bytes):
    fake_core.script = [
        ("tool", "transcribe_audio", {"attachment_id": "audio:0"}),
        ("tool", "describe_image",
         {"attachment_id": "image:0", "question": "Anything wrong?"}),
        ("answer", "Combined assessment ready."),
    ]
    r = client.post(
        "/api/chat",
        data={"text": "review this dictation and image"},
        files=[
            ("audio", ("d.wav", wav_bytes, "audio/wav")),
            ("images", ("img.png", png_bytes, "image/png")),
        ],
    )
    r.raise_for_status()
    j = r.json()
    assert j["answer"] == "Combined assessment ready."
    names = [c["name"] for c in j["tool_calls"]]
    assert "transcribe_audio" in names
    assert "describe_image" in names
    assert j["session_id"]


def test_chat_session_reset(client, fake_core):
    fake_core.script = [("answer", "ok"), ("answer", "ok")]
    r1 = client.post("/api/chat", data={"text": "hi", "session_id": "abc"})
    r1.raise_for_status()
    r2 = client.post("/api/sessions/abc/reset")
    assert r2.json()["ok"]
