# AranMed Core Architecture v1

**Status:** Rule Engine, Clinical Data Fabric, Knowledge Artifact pilot pipeline, and the frontend token/component redesign are implemented and test-covered as of this pass — see `backend/rules/`, `backend/fabric/`, `src/medrag/knowledge/{artifacts,pilot_sample}.py`, `frontend/styles/tokens.css`, `frontend/components/ui/`. The Knowledge Artifact pilot's *code* is complete and unit-tested against fixture data, but the actual ~1000-chunk classification run against the live Qdrant corpus has not been executed — that requires a reachable Qdrant/MedicalRAG deployment, which wasn't available in the environment this pass was implemented in. Run `python -m medrag.knowledge.pilot_sample` against a live stack to produce real results, then `python -m medrag.eval.artifact_pilot_eval --make-template` to start the eval gate.

Companion docs: [`CONFIGURATION.md`](CONFIGURATION.md) (no-hardcoding rule + every env var) · [`DEPLOYMENT.md`](DEPLOYMENT.md) (Docker, scaling, load balancing, measured capacity) · [`SYSTEM_OVERVIEW.md`](../SYSTEM_OVERVIEW.md) (what runs today, endpoint-by-endpoint) · [`MICROSERVICES.md`](../MICROSERVICES.md) (topology / env keys) · [`RULE_MODEL_SCHEMA_v1.md`](RULE_MODEL_SCHEMA_v1.md) · [`KNOWLEDGE_ARTIFACT_SCHEMA_v1.md`](KNOWLEDGE_ARTIFACT_SCHEMA_v1.md) · [`CLINICAL_DATA_FABRIC_v1.md`](CLINICAL_DATA_FABRIC_v1.md) · [`ROADMAP.md`](ROADMAP.md)

## 1. Reframing

AranMed is not "a radiology system." It is a **Clinical Intelligence Platform**, and the radiology dictation/reporting stack documented in `SYSTEM_OVERVIEW.md` is its **first reference implementation / vertical workflow**. Nothing about the running radiology system changes because of this document — this is a naming and layering clarification that determines where *new* shared capability goes, so the next vertical (e.g. emergency, cardiology) doesn't have to reinvent knowledge retrieval, rule evaluation, or patient data modeling from scratch.

```
                         ARANMED
              Clinical Intelligence Platform
                            │
                    ┌───────┴───────┐
                    │  ARANMED CORE │
                    └───────┬───────┘
                            │
       ┌────────────────────┼────────────────────┐
       ▼                    ▼                    ▼
 Knowledge Engine       Rule Engine       Clinical Data Fabric
       │                    │                    │
       └────────────────────┼────────────────────┘
                            ▼
                   AranMed Runtime / Orchestrator
                        (roadmap — see ROADMAP.md)
                            │
                            ▼
                  Clinical Workflow Engine
                        (roadmap)
                            │
                            ▼
                ┌───────────┴───────────┐
                ▼                       ▼
         Radiology Workflow      (future workflows)
         (built — this repo)         (roadmap)
```

## 2. What "Core" means concretely in this repo

The Core is not a new service and not a rewrite. It is three things that already existed in scattered form, now given a shared shape:

| Engine | Before this pass | After this pass |
|---|---|---|
| **Rule Engine** | 3 unrelated implementations: `backend/data/REPORT_RULES.json` (naming), `backend/clinical_safety.py` (regex critical-finding triage), `src/medrag/rules/engine.py` (4 hardcoded drug/allergy functions) | One declarative `Rule` schema + repository + evaluator in `backend/rules/`, with the 3 sources migrated in by value (see [`RULE_MODEL_SCHEMA_v1.md`](RULE_MODEL_SCHEMA_v1.md)). The legacy modules become thin shims so every existing caller and test keeps working unmodified. |
| **Knowledge Engine** | Qdrant + BGE-M3 + hybrid retrieval in `src/medrag/` — already a real knowledge engine, just chunk-shaped, not artifact-shaped | An additive `knowledge_artifacts` classification layer (pilot scale, ~1000 chunks) sits alongside the existing chunk/retrieval pipeline — see [`KNOWLEDGE_ARTIFACT_SCHEMA_v1.md`](KNOWLEDGE_ARTIFACT_SCHEMA_v1.md). Retrieval itself is untouched. |
| **Clinical Data Fabric** | Two parallel patient stores: SQLite `patients` table (`backend/db.py`, used by the real `/api/ehr/*` REST routes) and a file-based JSON store (`backend/tools/ehr.py`, used only by the agent tool-calling path) | SQLite becomes canonical; the file-based store is folded in. See [`CLINICAL_DATA_FABRIC_v1.md`](CLINICAL_DATA_FABRIC_v1.md). |

Runtime/Orchestrator (task-based model routing beyond the existing LRU governor in `backend/registry.py`) and a generic multi-specialty Workflow Engine are **not built in this pass** — they're documented as roadmap in [`ROADMAP.md`](ROADMAP.md) so future work has a landing spot without forcing a premature abstraction now.

## 3. Today's real topology (unchanged by this refactor)

```
frontend :3000 ──/api──▶ aranmed-backend :8010 ──HTTP──▶ MedicalRAG :8080 ──▶ Qdrant + vLLM/Ollama
                                  │
                                  └──▶ Ollama :11434 (report structuring, EHR build, education)
```

The backend and MedicalRAG remain two separate processes coupled only over HTTP (`backend/integrations/medrag_client.py`). The Rule Engine is **in-process** on each side (a `backend/rules/` copy for the backend's clinical-safety/naming checks, a thin shim in `src/medrag/rules/engine.py` for RAG rule-alert injection) rather than a new third service — introducing a shared rule *service* would add a network hop to the report-generation hot path, which is explicitly out of scope per the constraint that ASR/report-generation performance and behavior must not regress.

## 4. Non-negotiable constraint carried into every engine

The Whisper ASR pipeline (`backend/providers/*`) and the report-generation flow (`backend/app.py::transcribe/report/dictate`, `backend/templates.py`, `report_rules.py`'s official-title resolution) are **read-only** for all Core work in this pass. Every engine above is designed to wrap or sit beside that pipeline, never inside it:

- Rule Engine changes land in `clinical_safety.py`'s *internals* only; its public functions keep their exact signatures and are called from `app.py::report()` exactly as before.
- Clinical Data Fabric changes land in `store.py`/`tools/ehr.py`'s persistence layer; they do not touch how a report is generated.
- The Knowledge Artifact pilot writes to a new `catalog.db` table, never to the live Qdrant collections the RAG retrieval path reads from.

## 5. Radiology's place in this architecture

Radiology is not special-cased in the Core — it consumes the same Rule Engine, the same Qdrant/knowledge stack, and (once unified) the same patient store that any future vertical would. Concretely, today's pipeline maps onto the Core like this:

| Radiology concept (today) | Core concept |
|---|---|
| `backend/clinical_safety.py` critical-finding triage | Safety bank rules, evaluated by the Rule Engine |
| `backend/data/REPORT_RULES.json` naming rules | Documentation/Insurance bank rules |
| `backend/data/templates/*.txt` (56 templates) | Report-knowledge resources (unchanged, still files — no artifact migration planned for these in this pass) |
| `src/medrag` chunk corpus | Knowledge Engine's chunk layer, now with an additive artifact classification on a pilot sample |
| `backend/db.py::patients` / `backend/tools/ehr.py` | Clinical Data Fabric (unified this pass) |

No workflow engine or model router is required for Radiology to keep working — those stay roadmap items until a second vertical actually needs them, per the source feedback's explicit "don't build for a Workflow Engine you don't have a second workflow for yet" guidance.
