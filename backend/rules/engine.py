"""Rule Engine evaluator (docs/core/RULE_MODEL_SCHEMA_v1.md).

Deterministic; no LLM involved. Given a blob of text (and optional context
for the ``naming`` matcher), returns every rule that fires. This module has
no knowledge of *which* rules exist beyond what's loaded from
``backend/rules/banks/*.json`` via :mod:`backend.rules.repository`.
"""
from __future__ import annotations

from typing import Any

from . import repository
from .matcher import match_condition
from .schema import Rule, RuleHit


def _build_hit(rule: Rule, excerpt: str) -> RuleHit:
    action = rule.action or {}
    code = action.get("code") or rule.rule_id
    label = action.get("label") or code.replace("_", " ").title()
    fmt = {"excerpt": excerpt[:160], "label": code.replace("_", " ")}
    template_en = action.get("message_en") or action.get("message") or ""
    template_fa = action.get("message_fa")
    try:
        message = template_en.format(**fmt) if template_en else ""
    except (KeyError, IndexError):
        message = template_en
    try:
        message_fa = template_fa.format(**fmt) if template_fa else None
    except (KeyError, IndexError):
        message_fa = template_fa
    return RuleHit(
        rule_id=rule.rule_id,
        bank=rule.bank,
        severity=action.get("severity", "info"),
        code=code,
        label=label,
        message=message,
        message_fa=message_fa,
        excerpt=excerpt[:160],
        source=rule.source,
        data=action.get("data"),
    )


def evaluate(
    text: str,
    *,
    banks: list[str] | None = None,
    context: dict[str, Any] | None = None,
) -> list[RuleHit]:
    """Evaluate every active rule in *banks* (default: all banks) against
    *text*. ``context`` carries extra fields some matchers need (e.g.
    ``spoken`` for the ``naming`` matcher)."""
    if not (text or "").strip():
        return []
    ctx = context or {}
    hits: list[RuleHit] = []
    seen_codes: set[str] = set()
    for rule in repository.load_rules(banks=banks):
        try:
            ok, excerpt = match_condition(rule.condition, text, ctx)
        except Exception:  # noqa: BLE001
            continue
        if not ok:
            continue
        hit = _build_hit(rule, excerpt)
        if hit.code in seen_codes:
            continue
        seen_codes.add(hit.code)
        hits.append(hit)
    return hits
