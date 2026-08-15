"""Deterministic clinical rule engine (LLM explains; rules decide).

Rule content used to be hardcoded as Python functions in this module. It
now lives as data in ``src/medrag/rules/banks/{drug,clinical}.json``,
evaluated by the generic, rule-agnostic matcher in
:mod:`medrag.rules.matcher`. See docs/core/RULE_MODEL_SCHEMA_v1.md.

Every symbol this module exposed before the migration (`RuleAlert`,
`evaluate`, `egfr_metformin_warning`, `check_allergy`, `warfarin_amiodarone`,
`sepsis_news2_hint`, `RULES`) is preserved with the same signature/return
shape, since :mod:`medrag.rag.engine` and existing tests import them
directly.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .matcher import match_condition

_BANKS_DIR = Path(__file__).resolve().parent / "banks"
_ACTIVE_STATUSES = ("approved", "production")


@dataclass
class RuleAlert:
    rule_id: str
    severity: str  # info | warning | critical
    message: str
    message_fa: str | None = None
    data: dict | None = None


@lru_cache(maxsize=None)
def _load_bank(bank: str) -> tuple[dict, ...]:
    path = _BANKS_DIR / f"{bank}.json"
    if not path.is_file():
        return ()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        r for r in raw.get("rules", []) if r.get("status", "production") in _ACTIVE_STATUSES
    )


def _fire(bank: str, query: str) -> list[RuleAlert]:
    out: list[RuleAlert] = []
    for rule in _load_bank(bank):
        try:
            ok, excerpt = match_condition(rule["condition"], query)
        except Exception:  # noqa: BLE001
            continue
        if not ok:
            continue
        action = rule.get("action", {})
        fmt = {"excerpt": excerpt[:160]}
        msg_en = action.get("message_en", "")
        msg_fa = action.get("message_fa")
        try:
            message = msg_en.format(**fmt) if msg_en else ""
        except (KeyError, IndexError):
            message = msg_en
        try:
            message_fa = msg_fa.format(**fmt) if msg_fa else None
        except (KeyError, IndexError):
            message_fa = msg_fa
        out.append(
            RuleAlert(
                rule_id=action.get("code") or rule["rule_id"],
                severity=action.get("severity", "info"),
                message=message,
                message_fa=message_fa,
                data=action.get("data"),
            )
        )
    return out


def check_allergy(query: str) -> list[RuleAlert]:
    return [a for a in _fire("clinical", query) if a.rule_id == "allergy_beta_lactam"]


def egfr_metformin_warning(query: str) -> list[RuleAlert]:
    hits = [
        a
        for a in _fire("drug", query)
        if a.rule_id in ("metformin_egfr_lt30", "metformin_egfr_lt45")
    ]
    # Original behavior: lt30 and lt45 are mutually exclusive by construction
    # (banks/drug.json encodes lt45 as 30<=eGFR<45), so at most one fires.
    return hits


def warfarin_amiodarone(query: str) -> list[RuleAlert]:
    return [a for a in _fire("drug", query) if a.rule_id == "warfarin_amiodarone"]


def sepsis_news2_hint(query: str) -> list[RuleAlert]:
    return [a for a in _fire("clinical", query) if a.rule_id == "sepsis_screen"]


RULES = [
    check_allergy,
    egfr_metformin_warning,
    warfarin_amiodarone,
    sepsis_news2_hint,
]


def evaluate(query: str) -> list[dict]:
    out: list[RuleAlert] = []
    for fn in RULES:
        try:
            out.extend(fn(query))
        except Exception:  # noqa: BLE001
            continue
    return [asdict(a) for a in out]
