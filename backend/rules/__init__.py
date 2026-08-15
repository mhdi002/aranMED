"""AranMed backend Rule Engine — see docs/core/RULE_MODEL_SCHEMA_v1.md.

Public API: :func:`evaluate` (backend.rules.engine) and the ``Rule`` /
``RuleHit`` shapes in :mod:`backend.rules.schema`. Rule *content* lives only
in ``backend/rules/banks/*.json`` — this package contains generic,
rule-agnostic loading/matching code and no per-rule Python.
"""
from __future__ import annotations

from .engine import evaluate
from .schema import Rule, RuleHit

__all__ = ["evaluate", "Rule", "RuleHit"]
