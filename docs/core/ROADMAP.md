# AranMed Core Roadmap (documented, not built)

Companion doc: [`CORE_ARCHITECTURE_v1.md`](CORE_ARCHITECTURE_v1.md)

This document exists so that everything the source stakeholder feedback envisioned but that this pass deliberately did **not** build has a defined landing spot — future work extends this roadmap instead of re-deriving scope from scratch, and nothing here is implied to be "in progress" just because it's written down.

## Built this pass (see `CORE_ARCHITECTURE_v1.md` §2)

- Rule Engine consolidation (`backend/rules/`) — [`RULE_MODEL_SCHEMA_v1.md`](RULE_MODEL_SCHEMA_v1.md). 15 rules migrated (10 safety, 3 drug, 2 clinical), 2 naming meta-rules, 43+8 fixture tests, `GET /api/rules` introspection.
- Clinical Data Fabric unification (`backend/fabric/`) — [`CLINICAL_DATA_FABRIC_v1.md`](CLINICAL_DATA_FABRIC_v1.md). Agent tool path and REST path now share the SQLite `patients` store; `scripts/migrate_ehr_json_to_sqlite.py` for any pre-existing legacy files.
- Knowledge Artifact classification pilot pipeline, sized for ~1000 chunks — [`KNOWLEDGE_ARTIFACT_SCHEMA_v1.md`](KNOWLEDGE_ARTIFACT_SCHEMA_v1.md). Pipeline code + 27 tests complete; the actual run against the live Qdrant corpus is still pending a reachable deployment (see that doc's Status note).
- Spatial/minimalist frontend redesign — `frontend/styles/tokens.css` (spacing/type/color scale), `frontend/components/ui/` (Button/Card/Field/Modal/PageHeader/Badge/Toolbar/EmptyState/Spinner), real per-view routes (`frontend/components/AppShell.js`), `BackgroundFx` (glassmorphic photo/SVG backdrop) removed entirely, `window.confirm()` replaced by an accessible `Modal` in EHR/Reports/Settings. Verified against a live backend in both languages and both themes.

## Roadmap — not built this pass

### AranMed Runtime / Model Router

`backend/registry.py` already implements a real LRU + VRAM-budget eviction governor (`models.yaml` → `runtime.max_warm_models`, `runtime.vram_budget_gb`) — it decides *which loaded model to evict*, not *which model a given task needs*. The roadmap item is task-type-aware routing on top of that governor:

```
Task = simple classification  → small/no model
Task = search                 → embedding + reranker only, no generation
Task = structured report       → mid-size model (current default path)
Task = complex synthesis       → large model
Task = critical alert          → Rule Engine only, no LLM (already true today — see clinical_safety.triage_local)
```

Depends on: enough task-type signal existing in the codebase to route on (today, callers already know which model they want — `registry.get_text("core")`, `registry.get_asr(...)` — so a router needs a reason to exist beyond what static call sites already express). Revisit once a second workflow (see below) creates genuinely competing task types on the same GPU budget.

### Clinical Workflow Engine

A generic engine that takes `{user, patient, hospital, task, permissions}` and decides which knowledge/rules/data/workflow apply, with Radiology as the first of many pluggable workflows (Emergency, Cardiology, ICU, ...). Not built because there is currently exactly one workflow (Radiology) — an abstraction with a single implementation is speculative by definition. Build this when a second vertical is actually being added, informed by what that vertical's integration with the Core actually needs.

### Additional clinical specialties / verticals

Emergency, Cardiology, Neurology, Surgery, Oncology, ICU, etc. Each would be a new workflow module consuming the same Core (Knowledge Engine, Rule Engine, Clinical Data Fabric) that Radiology already consumes. None are scoped, scheduled, or started.

### Additional rule banks

`legal`, `hospital`, `workflow`, `data_quality` banks are named and reserved in [`RULE_MODEL_SCHEMA_v1.md`](RULE_MODEL_SCHEMA_v1.md) §3 but have zero rules. Populating them requires actual source material (e.g. Iranian Ministry of Health regulations for `legal`, per-institution policy documents for `hospital`) that hasn't been collected or reviewed — collecting it is out of scope here.

### Knowledge Artifact scale-up beyond the pilot

Going from the ~1000-chunk pilot to 10k → 100k → the full 446,576 chunks is explicitly gated on the pilot's precision/recall results in `reports/artifact_pilot_eval.md` (see [`KNOWLEDGE_ARTIFACT_SCHEMA_v1.md`](KNOWLEDGE_ARTIFACT_SCHEMA_v1.md) §6). No scale-up work is scheduled by this document.

### Interoperability adapters (CQL / FHIR Clinical Reasoning / HL7 / IHE Radiology)

The Rule Engine's `condition`/`action` shapes ([`RULE_MODEL_SCHEMA_v1.md`](RULE_MODEL_SCHEMA_v1.md) §4) are AranMed-internal. CQL/ELM export, FHIR Clinical Reasoning resource representation, and IHE Radiology profile support (Scheduled Workflow, Reporting Workflow, Evidence Documents) are treated as optional future *adapters* translating AranMed's internal rule/knowledge representation to/from those standards — not a requirement that AranMed's internal engine be built directly on top of them. No adapter work is scheduled.

### Lab / Imaging integration into the Clinical Data Fabric

The `Lab` and `Imaging` dataclasses exist in `backend/fabric/` ([`CLINICAL_DATA_FABRIC_v1.md`](CLINICAL_DATA_FABRIC_v1.md) §3) but nothing populates them yet — there's no lab-result ingestion path and no link from a generated radiology report back into a patient's `Imaging` list. Wiring the existing report-generation output into a patient's Fabric record is a natural next step but is not part of this pass, since it would touch the report-generation call path that this pass is explicitly not allowed to modify.

### Multi-jurisdiction rule scoping

`jurisdiction`/`institution` fields exist on every `Rule` but nothing currently resolves "which rule wins when both a universal and an Iran-specific version exist." All rules migrated in this pass are jurisdiction-`null` (universal), so the conflict this resolution logic would need to handle doesn't exist yet.
