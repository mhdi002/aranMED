"""Generic rule condition matchers (medrag's own copy).

medrag is an independently-deployable service (see docs/SYSTEM_OVERVIEW.md:
"no in-process imports of medrag from backend/" — and, symmetrically, medrag
must not depend on backend/ being on the filesystem either). This module is
therefore a self-contained duplicate of backend/rules/matcher.py's generic,
rule-agnostic matcher logic — not a rule-specific reimplementation. Adding a
rule is a data change in banks/*.json, never a code change here. See
docs/core/RULE_MODEL_SCHEMA_v1.md §4/§6.
"""
from __future__ import annotations

import re
from typing import Any

_OPS = {
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "eq": lambda a, b: a == b,
}


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
    m = re.search(cond["pattern"], text, re.I)
    if not m:
        return False, ""
    return True, m.group(0).strip()


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


def match_condition(cond: dict[str, Any], text: str) -> tuple[bool, str]:
    ctype = cond.get("type")
    if ctype == "regex":
        return _match_regex(cond, text)
    if ctype == "numeric":
        return _match_numeric(cond, text)
    if ctype == "conjunction":
        excerpts: list[str] = []
        for sub in cond.get("all", []):
            ok, excerpt = match_condition(sub, text)
            if not ok:
                return False, ""
            if excerpt:
                excerpts.append(excerpt)
        return True, "; ".join(excerpts)
    return False, ""
