"""Backend Rule Engine fixture tests (docs/core/RULE_MODEL_SCHEMA_v1.md).

Loads backend/rules/tests/<rule_id>.json ({"positives":[...],"negatives":[...]})
and asserts every positive fires that exact rule_id and every negative
fires nothing from the safety bank. This is the regression net for the
Rule Engine consolidation: it must reproduce the exact firing behavior the
previously-hardcoded `_CRITICAL_PATTERNS` had.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

FIXTURES_DIR = BACKEND / "rules" / "tests"


def _fixture_cases():
    cases = []
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        rule_id = path.stem
        data = json.loads(path.read_text(encoding="utf-8"))
        for text in data.get("positives", []):
            cases.append(pytest.param(rule_id, text, True, id=f"{rule_id}-positive"))
        for text in data.get("negatives", []):
            cases.append(pytest.param(rule_id, text, False, id=f"{rule_id}-negative"))
    return cases


@pytest.mark.parametrize("rule_id,text,should_fire", _fixture_cases())
def test_safety_rule_fixture(rule_id, text, should_fire):
    from rules.engine import evaluate

    hits = evaluate(text, banks=["safety"])
    fired_rule_ids = {h.rule_id for h in hits}
    if should_fire:
        assert rule_id in fired_rule_ids, f"expected {rule_id} to fire on: {text!r}"
    else:
        assert rule_id not in fired_rule_ids, f"did not expect {rule_id} to fire on: {text!r}"


def test_repository_loads_all_banks():
    from rules import repository
    from rules.schema import BANKS

    all_rules = repository.all_rules()
    banks_present = {r.bank for r in all_rules}
    # Every rule's bank must be one of the 9 declared banks (schema §3).
    assert banks_present <= set(BANKS)
    # safety/documentation/insurance are populated this pass.
    assert "safety" in banks_present
    assert len(repository.load_rules(banks=["safety"])) == 10


def test_no_hardcoded_rule_content_in_engine_module():
    """Guards the 'nothing hardcoded' constraint: engine.py must contain no
    per-rule literals (rule_id / code strings), only generic matcher code."""
    import inspect

    from rules import engine

    src = inspect.getsource(engine)
    for forbidden in ("tension_pneumothorax", "metformin", "warfarin", "SAFETY-"):
        assert forbidden not in src, f"engine.py must not hardcode {forbidden!r}"


def test_api_rules_endpoint(http_client, auth_headers):
    r = http_client.get("/api/rules", headers=auth_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] >= 10
    assert data["by_bank"]["safety"] == 10
    assert any(rule["rule_id"] == "SAFETY-TENSION-PTX-001" for rule in data["rules"])


def test_candidate_rules_are_not_evaluated_by_default(tmp_path, monkeypatch):
    """A rule with status=candidate must not fire even if its condition
    matches — see docs/core/RULE_MODEL_SCHEMA_v1.md §5 (lifecycle)."""
    from rules import repository

    banks_dir = tmp_path / "banks"
    banks_dir.mkdir()
    (banks_dir / "safety.json").write_text(
        json.dumps(
            {
                "bank": "safety",
                "rules": [
                    {
                        "rule_id": "TEST-CANDIDATE-001",
                        "bank": "safety",
                        "name": "test candidate rule",
                        "condition": {"type": "regex", "pattern": "zzz_test_marker"},
                        "action": {"severity": "critical", "code": "test_marker"},
                        "status": "candidate",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(repository, "_BANKS_DIR", banks_dir)
    repository.clear_cache()
    try:
        rules = repository.load_rules(banks=["safety"])
        assert rules == [], "candidate-status rules must not be in the default active set"
    finally:
        repository.clear_cache()
