"""Loads Rule bank files (backend/rules/banks/*.json) — see
docs/core/RULE_MODEL_SCHEMA_v1.md §6: this module reads rule *data*, it
never contains rule content itself.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .schema import DEFAULT_ACTIVE_STATUSES, Rule

log = logging.getLogger("backend.rules.repository")

_BANKS_DIR = Path(__file__).resolve().parent / "banks"

_cache: dict[str, list[Rule]] | None = None


def banks_dir() -> Path:
    return _BANKS_DIR


def clear_cache() -> None:
    global _cache
    _cache = None


def _load_all() -> dict[str, list[Rule]]:
    global _cache
    if _cache is not None:
        return _cache
    out: dict[str, list[Rule]] = {}
    for path in sorted(_BANKS_DIR.glob("*.json")):
        bank = path.stem
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("failed to parse rule bank %s: %s", path, e)
            continue
        rules = [Rule.from_dict(r) for r in raw.get("rules", [])]
        out[bank] = rules
    _cache = out
    return out


def load_rules(
    *,
    banks: list[str] | None = None,
    statuses: tuple[str, ...] = DEFAULT_ACTIVE_STATUSES,
) -> list[Rule]:
    """Return rules across the requested banks (default: all), filtered to
    *statuses* (default: only approved/production — see schema §2/§5)."""
    all_banks = _load_all()
    selected = banks if banks is not None else list(all_banks.keys())
    out: list[Rule] = []
    for bank in selected:
        for rule in all_banks.get(bank, []):
            if rule.status in statuses:
                out.append(rule)
    return out


def all_rules(*, statuses: tuple[str, ...] | None = None) -> list[Rule]:
    """Every rule in every bank, regardless of status by default — used by
    GET /api/rules for introspection, not by evaluation."""
    all_banks = _load_all()
    out: list[Rule] = [r for rules in all_banks.values() for r in rules]
    if statuses is not None:
        out = [r for r in out if r.status in statuses]
    return out
