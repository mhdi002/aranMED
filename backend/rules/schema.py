"""Rule Model Schema v1 (docs/core/RULE_MODEL_SCHEMA_v1.md).

Pure data shapes. This module defines what a rule *looks like*; it never
contains rule content itself — that lives in ``backend/rules/banks/*.json``.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

BANKS = (
    "safety",
    "drug",
    "clinical",
    "documentation",
    "insurance",
    "legal",
    "hospital",
    "workflow",
    "data_quality",
)

STATUSES = ("candidate", "validated", "approved", "production")

# Only rules at these statuses are evaluated by default — see
# docs/core/RULE_MODEL_SCHEMA_v1.md §2 (status field) and §5 (lifecycle).
DEFAULT_ACTIVE_STATUSES = ("approved", "production")


@dataclass
class Rule:
    rule_id: str
    bank: str
    name: str
    condition: dict[str, Any]
    action: dict[str, Any]
    source: str = ""
    jurisdiction: str | None = None
    institution: str | None = None
    version: str = "1.0"
    effective_from: str | None = None
    effective_to: str | None = None
    status: str = "production"
    evidence_level: str | None = None
    validated_by: str | None = None
    dependencies: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Rule":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class RuleHit:
    """One rule firing against a given piece of text/context."""

    rule_id: str
    bank: str
    severity: str
    code: str
    label: str
    message: str
    message_fa: str | None
    excerpt: str
    source: str
    data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
