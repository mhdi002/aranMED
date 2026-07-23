"""Unit + API tests for clinical safety and template-mismatch awareness.

Runs without MedicalRAG / GPU by default (local triage + fake core LLM).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("ASR_AGENT_REGISTRY_YAML", str(BACKEND / "models.test.yaml"))
os.environ["CLINICAL_SAFETY_USE_MEDRAG"] = "0"
os.environ["CLINICAL_SAFETY_ALWAYS_MEDRAG"] = "0"
os.environ["REPORT_RULES_MEDRAG"] = "0"


def test_triage_critical_tension_ptx():
    from clinical_safety import triage_local

    text = (
        "Findings: large right tension pneumothorax with mediastinal shift. "
        "Impression: life-threatening tension pneumothorax."
    )
    alerts = triage_local(text)
    assert alerts, "expected non-empty critical_alerts"
    assert any(a["severity"] == "critical" for a in alerts)
    assert any("pneumothorax" in a["code"] for a in alerts)


def test_triage_non_critical_no_false_alarm():
    from clinical_safety import triage_local

    text = (
        "This is a chest sonography. Lungs are clear. No pneumothorax. "
        "Heart size normal. Impression: no acute cardiopulmonary process."
    )
    alerts = triage_local(text)
    assert alerts == [], f"false alarm: {alerts}"


def test_template_mismatch_spoken_vs_selected():
    from clinical_safety import assess_template_mismatch

    transcript = (
        "Using template Brain Sonography. Patient is a 40-year-old with headache. "
        "Findings: mild sinus mucosal thickening. Impression: no acute abnormality."
    )
    meta = assess_template_mismatch(transcript, "chest")
    assert meta["mismatch"] is True
    assert meta["selected_wins"] is True
    assert meta["spoken_template"]
    spoken = (meta["spoken_template"] or "").lower()
    assert "brain" in spoken or meta["matched_template_id"] == "brain"


def test_naming_rule_forbidden_neck_ct(tmp_path, monkeypatch):
    import json

    import report_rules
    from clinical_safety import assess_template_mismatch

    rules = {
        "official_titles": {"soft_tissue": "SPIRAL CT SCAN OF NECK SOFT TISSUE"},
        "aliases": {"neck ct": "SPIRAL CT SCAN OF NECK SOFT TISSUE"},
        "forbidden_titles": ["neck ct"],
    }
    p = tmp_path / "REPORT_RULES.json"
    p.write_text(json.dumps(rules), encoding="utf-8")
    monkeypatch.setenv("REPORT_RULES_PATH", str(p))
    report_rules.clear_rules_cache()

    meta = assess_template_mismatch(
        "Exam is neck ct. Soft tissues unremarkable.",
        "soft_tissue",
    )
    assert meta["naming_violations"], meta
    assert any(v["type"] == "forbidden_title" for v in meta["naming_violations"])
    report_rules.clear_rules_cache()


@pytest.mark.asyncio
async def test_enrich_report_payload_critical_and_mismatch():
    from clinical_safety import enrich_report_payload

    transcript = (
        "This is a Brain Sonography study. Findings demonstrate a large intracerebral "
        "hemorrhage with uncal herniation — critical finding, notify clinician STAT."
    )
    report = (
        "Chest sonography:\nFINDINGS: Large ICH with uncal herniation.\n"
        "IMPRESSION: Life-threatening cerebral herniation."
    )
    out = await enrich_report_payload(
        transcript=transcript,
        report_text=report,
        template_id="chest",
        use_medrag=False,
    )
    assert out["critical_alerts"], out
    assert out["template_mismatch"]["mismatch"] is True


def test_api_report_critical_and_mismatch(http_client, fake_core):
    """POST /api/report returns critical_alerts + template_mismatch metadata."""
    fake_core.script = [
        (
            "answer",
            "Chest sonography:\nFINDINGS: Tension pneumothorax with shift.\n"
            "IMPRESSION: Life-threatening tension pneumothorax.",
        )
    ]
    transcript = (
        "Using template Brain Sonography. There is a tension pneumothorax with mediastinal "
        "shift — immediately life-threatening. Call the clinician now."
    )
    r = http_client.post(
        "/api/report",
        json={"transcript": transcript, "template_id": "chest"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("report")
    assert data.get("critical_alerts"), data
    assert data.get("template_mismatch", {}).get("mismatch") is True


def test_api_report_non_critical(http_client, fake_core):
    fake_core.script = [
        (
            "answer",
            "Chest sonography:\nFINDINGS: No pleural effusion. No pneumothorax.\n"
            "IMPRESSION: No acute process.",
        )
    ]
    transcript = (
        "This is a chest sonography. No pleural effusion. No pneumothorax. "
        "Diaphragmatic movement is normal."
    )
    r = http_client.post(
        "/api/report",
        json={"transcript": transcript, "template_id": "chest"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("critical_alerts") == []
    mm = data.get("template_mismatch") or {}
    assert mm.get("mismatch") is False
