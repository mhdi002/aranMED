"""medrag Rule Engine migration regression tests (docs/core/RULE_MODEL_SCHEMA_v1.md).

Confirms src/medrag/rules/engine.py's data-driven banks/{drug,clinical}.json
reproduce the exact behavior the previously-hardcoded rule functions had.
"""
from __future__ import annotations

from medrag.rules.engine import (
    RuleAlert,
    check_allergy,
    egfr_metformin_warning,
    evaluate,
    sepsis_news2_hint,
    warfarin_amiodarone,
)


def test_egfr_metformin_lt30_critical():
    alerts = egfr_metformin_warning("Start metformin, eGFR=25")
    assert alerts
    assert isinstance(alerts[0], RuleAlert)
    assert alerts[0].severity == "critical"
    assert alerts[0].rule_id == "metformin_egfr_lt30"


def test_egfr_metformin_lt45_warning_not_lt30():
    alerts = egfr_metformin_warning("Continue metformin, eGFR=40")
    assert alerts
    assert alerts[0].severity == "warning"
    assert alerts[0].rule_id == "metformin_egfr_lt45"
    # Mutually exclusive with lt30, matching pre-refactor early-return behavior.
    assert not any(a.rule_id == "metformin_egfr_lt30" for a in alerts)


def test_egfr_metformin_normal_no_alert():
    assert egfr_metformin_warning("Continue metformin, eGFR=70") == []


def test_warfarin_amiodarone_interaction():
    alerts = warfarin_amiodarone("Patient on warfarin and amiodarone for AF")
    assert alerts and alerts[0].rule_id == "warfarin_amiodarone"
    assert alerts[0].severity == "warning"


def test_allergy_beta_lactam_conflict():
    alerts = check_allergy("Patient allergic to penicillin, prescribe amoxicillin")
    assert alerts and alerts[0].rule_id == "allergy_beta_lactam"
    assert alerts[0].severity == "critical"


def test_sepsis_screen_hint():
    alerts = sepsis_news2_hint("Patient with sepsis, check NEWS2")
    assert alerts and alerts[0].rule_id == "sepsis_screen"
    assert alerts[0].severity == "info"


def test_evaluate_aggregates_all_banks():
    all_alerts = evaluate("Warfarin plus amiodarone for AF")
    assert any(a["rule_id"] == "warfarin_amiodarone" for a in all_alerts)


def test_evaluate_returns_plain_dicts():
    all_alerts = evaluate("Start metformin, eGFR=20")
    assert all_alerts
    assert isinstance(all_alerts[0], dict)
    assert set(all_alerts[0].keys()) == {"rule_id", "severity", "message", "message_fa", "data"}
