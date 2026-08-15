"""Generic rule condition matchers.

Every matcher here is data-driven and rule-agnostic: adding, changing, or
retiring a rule is always an edit to ``backend/rules/banks/*.json``, never a
code change in this module. See docs/core/RULE_MODEL_SCHEMA_v1.md §4/§6.
"""
from __future__ import annotations

import re
from typing import Any

_NEGATION_NEAR = re.compile(
    r"\b(no|without|denies|negative\s+for|ruled\s+out|absence\s+of|"
    r"not\s+(seen|identified|present))\b|بدون|ندارد|منفی",
    re.I,
)

_OPS = {
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "eq": lambda a, b: a == b,
}


def _window_negated(text: str, start: int, end: int, radius: int) -> bool:
    if radius <= 0:
        return False
    left = max(0, start - radius)
    return bool(_NEGATION_NEAR.search(text[left:end]))


def _extract_number(text: str, patterns: list[str]) -> float | None:
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                return float(m.group(1))
            except (ValueError, IndexError):
                continue
    return None


def _match_regex(cond: dict[str, Any], text: str) -> tuple[bool, str]:
    radius = int(cond.get("negation_window") or 0)
    for m in re.finditer(cond["pattern"], text, re.I):
        if _window_negated(text, m.start(), m.end(), radius):
            continue
        return True, m.group(0).strip()
    return False, ""


def _match_numeric(cond: dict[str, Any], text: str) -> tuple[bool, str]:
    value = _extract_number(text, cond["extract"])
    if value is None:
        return False, ""
    op = _OPS.get(cond.get("op", "eq"))
    if op is None:
        return False, ""
    if not op(value, float(cond["value"])):
        return False, ""
    return True, (str(int(value)) if value == int(value) else str(value))


def _match_naming(cond: dict[str, Any], ctx: dict[str, Any]) -> tuple[bool, str]:
    """Delegates to backend.report_rules — used only by documentation/insurance
    meta-rules for introspection (GET /api/rules), never by the report
    generation path itself. See docs/core/RULE_MODEL_SCHEMA_v1.md §4."""
    spoken = ctx.get("spoken")
    if not spoken:
        return False, ""
    try:
        import report_rules
    except Exception:  # noqa: BLE001
        return False, ""
    check = cond.get("check")
    if check == "forbidden_title":
        return report_rules.is_forbidden_title(spoken), spoken
    if check == "official_title_required":
        return report_rules.resolve_alias(spoken) is None, spoken
    return False, ""


def match_condition(cond: dict[str, Any], text: str, ctx: dict[str, Any]) -> tuple[bool, str]:
    """Evaluate one declarative condition against *text* (+ optional *ctx*).

    Returns ``(matched, excerpt)``. ``excerpt`` is the matched span for a
    single condition, or the ``"; "``-joined excerpts of a conjunction's
    sub-conditions.
    """
    ctype = cond.get("type")
    if ctype == "regex":
        return _match_regex(cond, text)
    if ctype == "numeric":
        return _match_numeric(cond, text)
    if ctype == "naming":
        return _match_naming(cond, ctx)
    if ctype == "conjunction":
        excerpts: list[str] = []
        for sub in cond.get("all", []):
            ok, excerpt = match_condition(sub, text, ctx)
            if not ok:
                return False, ""
            if excerpt:
                excerpts.append(excerpt)
        return True, "; ".join(excerpts)
    return False, ""
