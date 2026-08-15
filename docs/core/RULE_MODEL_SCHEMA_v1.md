# Rule Model Schema v1

Companion docs: [`CORE_ARCHITECTURE_v1.md`](CORE_ARCHITECTURE_v1.md) · [`SYSTEM_OVERVIEW.md`](../SYSTEM_OVERVIEW.md) §9–10 (existing alert/template-mismatch behavior this schema formalizes)

Implementation: `backend/rules/` (see that package's own module docstrings for the current source of truth — this document is the spec, the code is the implementation).

## 1. Why a shared schema

Before this pass, "rules" meant three unrelated things in this codebase: a JSON dictionary of insurance exam titles, a list of compiled regexes with a negation-window heuristic, and four hand-written Python functions. None of them shared a severity taxonomy, a version, a jurisdiction, or a way to say "this rule is still a draft, don't act on it yet." That made it impossible to answer simple questions like "what rules fired for this report, and why" in a uniform way, and it meant every new rule needed a new code path.

This schema does **not** change what any existing rule *does*. Every current rule (10 critical-finding patterns, 4 drug/allergy checks, the naming/insurance-title rules) is migrated in **by value** — same trigger conditions, same messages, same severities — behind the same public functions that already exist (`clinical_safety.triage_local`, `medrag.rules.engine.evaluate`, etc.). This is a consolidation of representation, not a rewrite of clinical logic.

## 2. The `Rule` shape

```json
{
  "rule_id": "SAFETY-TENSION-PTX-001",
  "bank": "safety",
  "name": "Tension pneumothorax mention",
  "condition": {
    "type": "regex",
    "pattern": "\\b(tension\\s+pneumothorax|pneumothorax\\s+under\\s+tension)\\b|پنوموتوراکس\\s*تحت\\s*فشار",
    "negation_window": 40
  },
  "action": {
    "type": "alert",
    "severity": "critical",
    "code": "tension_pneumothorax",
    "message_en": "Critical / potentially life-threatening finding detected (tension pneumothorax): «{excerpt}»",
    "message_fa": null
  },
  "source": "backend/clinical_safety.py::_CRITICAL_PATTERNS (pre-refactor)",
  "jurisdiction": null,
  "institution": null,
  "version": "1.0",
  "effective_from": "2026-08-14",
  "effective_to": null,
  "status": "production",
  "evidence_level": null,
  "validated_by": "migrated-from-existing-production-code",
  "dependencies": [],
  "tags": ["radiology", "critical-finding"]
}
```

| Field | Type | Notes |
|---|---|---|
| `rule_id` | string | Stable, human-readable, never reused. |
| `bank` | enum | One of the 9 banks below. |
| `condition` | object | Declarative matcher — see §4. Never raw Python in the rule file itself. |
| `action` | object | `type: alert \| block \| naming_violation`; carries `severity`, `code`, bilingual message templates. |
| `source` | string | Free text provenance — which legacy module/line this came from, or which guideline/document for future-authored rules. |
| `jurisdiction` | string\|null | e.g. `"IR"`. `null` = universal. Not populated for the migrated rules (they weren't jurisdiction-scoped before either). |
| `institution` | string\|null | Hospital-specific override scope. Unused this pass — reserved for `ROADMAP.md`'s Hospital Rule Bank. |
| `version` | string | Semver-ish string, bumped on any condition/action change. |
| `effective_from` / `effective_to` | date\|null | Validity window. |
| `status` | enum | `candidate \| validated \| approved \| production`. **Only `approved`/`production` rules are evaluated by default** — this is how the Knowledge Artifact pilot's `RULE_CANDIDATE` outputs (see [`KNOWLEDGE_ARTIFACT_SCHEMA_v1.md`](KNOWLEDGE_ARTIFACT_SCHEMA_v1.md)) can be written to a bank without silently going live. |
| `evidence_level` | string\|null | e.g. guideline grade, for future-authored clinical rules. Not populated for migrated rules. |
| `validated_by` | string | Who/what approved this version. Migrated rules use the literal marker `"migrated-from-existing-production-code"` so it's auditable which rules were carried over vs newly authored. |
| `dependencies` | array | Other `rule_id`s this rule assumes have already run (empty for all rules in this pass — no rule chaining exists yet). |
| `tags` | array | Free-form, used for filtering/reporting only. |

## 3. The 9 banks

Only 3 are populated in this pass (mapped from existing code); the other 6 are named and reserved so future rule authoring has a defined home, per [`ROADMAP.md`](ROADMAP.md).

| Bank | Populated this pass? | Source |
|---|---|---|
| `safety` | Yes | `clinical_safety.py::_CRITICAL_PATTERNS` (10 rules) |
| `drug` | Yes | `medrag/rules/engine.py` (3 of 4: eGFR/metformin ×2 severities, warfarin/amiodarone) |
| `clinical` | Yes | `medrag/rules/engine.py` (allergy check, sepsis-NEWS2 hint) |
| `documentation` | Partially — naming rules only | `backend/data/REPORT_RULES.json` via a `naming` condition type (§4), not hand-migrated entry-by-entry |
| `insurance` | Partially — same source as `documentation` | ditto |
| `legal` | No | Reserved — roadmap |
| `hospital` | No | Reserved — roadmap |
| `workflow` | No | Reserved — roadmap |
| `data_quality` | No | Reserved — roadmap |

## 4. Condition matcher types

Declarative, not code, so rules stay data (auditable, diffable, no redeploy needed to add one) — but only the matcher types actually needed to represent *today's* logic are implemented; more are added when a real rule needs them, not speculatively.

- **`regex`** — `{pattern, negation_window?}`. Reuses `clinical_safety._window_negated`'s exact negation-scan behavior (search N chars before the match for a negation cue like "no", "denies", "بدون").
- **`numeric`** — `{extract: [pattern, ...], var, op, value}`. Reuses the extraction approach already in `medrag/rules/engine.py::_num()` for pulling a lab value (e.g. eGFR) out of free text before comparing.
- **`conjunction`** — `{all: [condition, ...]}`. For rules like allergy-vs-medication that need two independent matches to both hold.
- **`naming`** — `{check: "forbidden_title" | "official_title_required"}`. Delegates to the existing `report_rules.py` helpers (`is_forbidden_title`, `official_title_for`, `resolve_alias`) rather than re-encoding the hundreds of official titles as rule data — `REPORT_RULES.json` stays the source of truth for the title catalog itself.

## 5. Lifecycle

```
source document / legacy code
        ↓
   candidate           ← Knowledge Artifact pilot's RULE_CANDIDATE output lands here
        ↓
  human validation
        ↓
   test cases          ← backend/rules/tests/<rule_id>.json: {positives:[...], negatives:[...]}
        ↓
    approved
        ↓
   production           ← evaluated by default
```

All 3 legacy sources are migrated directly to `status: "production"` (they were already live in production before this refactor) with `validated_by: "migrated-from-existing-production-code"` — no re-validation gate for logic that was already shipping, since the goal is representation parity, not re-litigating existing clinical decisions.

## 6. One representation across every lifecycle stage — nothing hardcoded

A rule is **one JSON object in `backend/rules/banks/*.json`**, from the moment it's a `candidate` through `production`. Moving a rule between lifecycle stages (§5) is a metadata edit — `status` (and usually `version`) change — to that same file, never a re-encoding into Python, a separate "draft rules" code path, or a hardcoded conditional in `engine.py`. Concretely, this constrains the implementation in `backend/rules/`:

- `repository.py` only ever loads rule *data* from `banks/*.json`. It contains no rule content itself — no bank, no `rule_id`, no condition, no message string is ever written directly into `repository.py` or `engine.py` as Python literals.
- `engine.py` contains exactly the 4 matcher-type evaluators from §4 (`regex`, `numeric`, `conjunction`, `naming`) and nothing rule-specific. Adding rule #11 to the safety bank must never require a code change to `engine.py` — only a new entry in `banks/safety.json`.
- The legacy-module shims (`clinical_safety.py`, `medrag/rules/engine.py`) do not keep parallel hardcoded copies of the patterns they used to own — once migrated, `_CRITICAL_PATTERNS` and `RULES` cease to exist as Python literals and become read-only views computed from `banks/*.json` at import/call time, so there is exactly one place a rule's condition and message can be edited.
- Bank files are the audit trail: `git log` on `banks/safety.json` shows every version of every rule, including stage transitions, without needing a separate rule-history table.

This is the concrete meaning of "preserve the rule at every stage, nothing hardcoded": the data/code boundary is absolute — rule *content* is always data, rule *evaluation mechanics* are the only thing that's code, and that code is generic across all rules in a bank rather than branching per `rule_id`.

## 7. What this schema explicitly does not do (yet)

- No rule chaining/dependency execution (the `dependencies` field is descriptive only).
- No hospital/jurisdiction-specific rule override resolution (fields exist, resolution logic doesn't).
- No CQL/FHIR Clinical Reasoning export — `condition`/`action` are AranMed-internal shapes. An adapter is a roadmap item, not a day-one requirement (see [`ROADMAP.md`](ROADMAP.md)).
