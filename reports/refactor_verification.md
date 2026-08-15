# AranMed Core Refactor + UI Redesign — Verification Report

Companion: [`../docs/core/CORE_ARCHITECTURE_v1.md`](../docs/core/CORE_ARCHITECTURE_v1.md) · baseline: [`full_system_verify.md`](full_system_verify.md)

This is the M7 gate for the engagement covering: Rule Engine consolidation (M1), Clinical Data Fabric unification (M2), Knowledge Artifact pilot pipeline (M3), and the spatial/minimalist frontend redesign (M4/M5). Per the explicit constraint carried through every milestone, **the Whisper ASR pipeline and report-generation logic were never modified** — every check below that exercises them is a regression check against that constraint, not new-feature testing.

## 1. Functional

### 1a. Automated test suites (unit + integration, no live services required)

| Suite | Result |
|---|---|
| `pytest tests/` (backend, excludes `test_live.py`/`test_real_pipeline.py` which require live Ollama/MedRAG/Triton) | **101 passed / 1 failed** in 27.5s |
| `pytest tests/medrag/` (excludes `test_system_smoke.py`, a live-service smoke test) | **90 passed** in 2.7s |

The one failure, `tests/test_api.py::test_chat_session_reset`, requires a reachable MedicalRAG service (`503 MedicalRAG unavailable`) and is **not caused by this refactor** — confirmed by `git stash` + re-running the same test against the pre-refactor tree, where it fails identically (see session transcript). No other regressions across either suite.

New coverage added this pass, all included in the counts above: `tests/test_rules_engine.py` (44), `tests/medrag/test_rules_engine.py` (8), `tests/test_fabric_ehr_unification.py` (3), `tests/medrag/test_knowledge_artifacts.py` (20), `tests/medrag/test_artifact_pilot_eval.py` (7).

### 1b. Real end-to-end run against a live backend (not mocked)

Backend started with the production model registry (`backend/models.yaml` — real `whisper-large-v3` via `hf_asr`, real Ollama `qwen3.5-9b`), MedicalRAG intentionally not running (matches the documented baseline in `full_system_verify.md`, which also ran with MedRAG down).

| Check | Result |
|---|---|
| `GET /api/health` | `ok:true`, `asr_model:whisper-large-v3`, `ollama_model:qwen3.5-9b`, `ollama_available:true` |
| `POST /api/transcribe` with real dictation audio (`Feb 3, 5.12 PM.m4a`, the same fixture used in the original baseline) | 200 OK, 241-char coherent English transcript of a hepatobiliary sonography dictation — **ASR pipeline unchanged and fully functional** |
| `POST /api/report` on that transcript, `template_id=hepatobiliary` | 200 OK, well-formed structured report, `critical_alerts: []`, `template_mismatch.mismatch: false` — correct (no critical findings, no mismatch) |
| `POST /api/report` with a tension-pneumothorax trigger phrase, `template_id=chest` | 200 OK, `critical_alerts` fired both `tension_pneumothorax` and `lethal_or_critical_flag` (`severity: critical`) via the migrated Rule Engine, `template_mismatch.mismatch: false` (spoken "chest sonography" correctly resolved to the selected `chest` template) — **byte-identical firing behavior to the pre-refactor hardcoded regex list** |
| Frontend: full login → role-select → `/dictate` → sidebar navigation to `/ehr` | Real 8-record patient list loaded from the **unified SQLite store** (proving M2 works against genuinely pre-existing production data, not just fixtures) |
| Frontend: EHR "Delete" → new `Modal` (replaces `window.confirm()`) | `role="dialog"` with correct title/body/Cancel/Delete rendered and functioned correctly |
| Frontend: language toggle EN→FA | Full RTL layout flip + Persian translation of nav, placeholders, buttons |
| Frontend: theme toggle light→dark | `data-theme="dark"` applied; computed `backdrop-filter: none` confirmed on `.card` — no glass/blur anywhere, confirming the minimalism goal |
| Frontend: production build (`next build`) | 14/14 pages compile and prerender cleanly, no errors |

## 2. Performance (no regressions vs. baseline)

| Measurement | Result |
|---|---|
| `backend/rules/repository.load_rules()`, cold (loads all 6 populated banks) | **0.80 ms** |
| `backend/rules/engine.evaluate()`, warm, safety bank (10 rules) | **0.038 ms/call** (1000-run average) |
| `medrag/rules/engine.evaluate()`, warm, drug+clinical banks (5 rules) | **0.028 ms/call** (1000-run average) |
| `POST /api/transcribe`, real audio, cold model load | 37.7 s (consistent with the documented Whisper-large-v3 cold-start cost — not a regression, no code in the ASR path changed) |
| `POST /api/report`, cold Ollama call | 41.4 s |
| `POST /api/report`, warm Ollama call (critical-alert case) | 6.8 s |

The Rule Engine consolidation (M1) adds sub-millisecond overhead — three orders of magnitude below the ASR/LLM latencies that dominate the actual request path. No measurable regression from replacing hardcoded regex lists with the generic bank-driven evaluator.

## 3. Quality

- **Rule Engine fidelity:** all 15 migrated rules (10 safety + 3 drug + 2 clinical) reproduce their pre-refactor firing conditions exactly — verified both by the 52 new fixture tests (`test_rules_engine.py` ×2) and by the live `/api/report` critical-alert run above. `test_no_hardcoded_rule_content_in_engine_module` and `test_candidate_rules_are_not_evaluated_by_default` in `tests/test_rules_engine.py` specifically guard the "nothing hardcoded" / lifecycle-status constraints from `docs/core/RULE_MODEL_SCHEMA_v1.md`.
- **Knowledge Artifact pilot:** classifier and storage logic covered by 27 tests against fixture chunk payloads (drug/rule-candidate/safety/definition/guideline/report-knowledge/other). **Not yet run against the live 446k-chunk Qdrant corpus** — no reachable Qdrant/MedicalRAG deployment in this environment. `reports/artifact_pilot_eval.md` will only exist once that run happens; this is the explicit, intentional stopping point documented in `docs/core/KNOWLEDGE_ARTIFACT_SCHEMA_v1.md` §6 (no scale-up without a human-reviewed eval).
- **UI accessibility:** `window.confirm()` fully removed (3 call sites: EHR delete, Reports clear-all, Settings reset), replaced with a real `role="dialog"` `aria-modal` component, verified interactively. No `backdrop-filter`/glass remains anywhere in the stylesheet (verified via computed style in a live dark-mode session, not just source inspection).

## 4. What this does not cover

- Live MedicalRAG-dependent flows (`/ask`, knowledge chat, EHR-ask, education-explain) — no MedicalRAG service was reachable in this environment; those code paths were not touched by this refactor and their pre-existing test coverage (`tests/medrag/test_rag_advanced.py`, `test_index_integrity.py`, all 17 passing) is unaffected.
- The Knowledge Artifact pilot's actual classification run and eval report against real corpus data (see §3).
- Load/concurrency testing — out of scope for this pass; the baseline `full_system_verify.md` also only covers single-request latency.
