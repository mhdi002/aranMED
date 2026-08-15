# Knowledge Artifact Schema v1

Companion docs: [`CORE_ARCHITECTURE_v1.md`](CORE_ARCHITECTURE_v1.md) · [`RULE_MODEL_SCHEMA_v1.md`](RULE_MODEL_SCHEMA_v1.md) (where `RULE_CANDIDATE` artifacts feed)

Implementation: `src/medrag/knowledge/{artifact_schema.py, artifacts.py, pilot_sample.py}`. Evaluation: `src/medrag/eval/artifact_pilot_eval.py` → `reports/artifact_pilot_eval.md`.

## 1. What this is, and what it deliberately is not

MedicalRAG already indexes 446,576 chunks in Qdrant with a genuinely rich payload (`text, title, book, specialty, language, doc_type, source_corpus, page, section_title, topic, evidence_level, recommendation_class, population, index_tags, active, …` — see `src/medrag/index/build_index.py::upsert_chunks`). That payload already answers "where did this text come from." It does not answer "what *kind* of clinical knowledge is this chunk" — a dosing table, a diagnostic criterion, a plain definition, and a raw guideline paragraph all look identical to retrieval today.

This pass adds a **classification layer on top of a 1000-chunk pilot sample**, not a re-ingestion, not a schema change to the live collections, and not a full-corpus classification run. The source feedback that motivated this work is explicit that a 446k-chunk classification pass is premature until a small pilot proves the taxonomy and classifier are good enough to trust — this document and its implementation stop exactly at that pilot boundary.

## 2. The 8 artifact types

| Type | Meaning | Example chunk content |
|---|---|---|
| `FACT` | A stated clinical fact with no recommendation attached | "The pancreas is retroperitoneal." |
| `DEFINITION` | Terminology / concept definition | "Hydronephrosis is dilation of the renal pelvis and calyces." |
| `GUIDELINE` | A named society/body recommendation | "ACR recommends follow-up CT at 6 months for..." |
| `PROTOCOL` | A stepwise procedure | "Contrast administration protocol: ..." |
| `DRUG_INFORMATION` | Dosing, interaction, contraindication content | "Metformin is contraindicated when eGFR < 30." |
| `SAFETY_INFORMATION` | Warnings, critical-finding language, contraindications not drug-specific | "Tension pneumothorax requires immediate decompression." |
| `REPORT_KNOWLEDGE` | Reporting-style/structure/terminology content, not diagnostic content | Radiology report template language, naming conventions |
| `RULE_CANDIDATE` | Text that reads as an executable rule ("if X then Y", dose thresholds, contraindication statements) but hasn't been reviewed | Feeds `backend/rules/banks/*.json` as `status: "candidate"` — see [`RULE_MODEL_SCHEMA_v1.md`](RULE_MODEL_SCHEMA_v1.md) §5 |

A ninth catch-all `OTHER` exists for anything the classifier can't confidently place — this is expected and tracked in the eval report, not treated as classifier failure by itself.

## 3. Storage: additive, non-destructive

```
Qdrant medical_library_* collections   ← UNCHANGED. Retrieval reads only this.
        │
        │  (chunk_id is stable and shared)
        ▼
catalog.db :: knowledge_artifacts       ← NEW table, this pass
  chunk_id       TEXT PRIMARY KEY   -- FK by value to the Qdrant payload's chunk_id
  artifact_type  TEXT               -- one of the 9 types above
  confidence     REAL
  rationale      TEXT               -- short human-readable "why this type"
  classifier     TEXT               -- "heuristic_v1" | "llm_v1"
  source_corpus  TEXT               -- copied from the chunk payload, for reporting without a join back to Qdrant
  specialty      TEXT
  rule_candidate INTEGER            -- 1 if artifact_type == RULE_CANDIDATE, denormalized for fast filtering
  created_at     TEXT
```

Classification results are **never** written back into the Qdrant point payload in this pass. This keeps the pilot fully reversible (drop one SQLite table) and guarantees zero risk to the live retrieval path that `src/medrag/rag/*` depends on.

## 4. Pilot sample selection (~1000 chunks)

`pilot_sample.py` pulls via Qdrant **scroll** (payload only, no vector fetch — cheap) stratified across:

- All 5 collections (`medical_library_main/standards/expand/expand2/expand3`).
- `specialty`, `doc_type`, `source_corpus`, and `language` (EN + FA), so the pilot doesn't accidentally validate only on one corpus.

The sampled id list is persisted to `results/artifact_pilot_sample.json` so the eval run is reproducible and auditable.

## 5. Classifier — two tiers, heuristic first

1. **Heuristic v1** (`classifier: "heuristic_v1"`) — reuses existing signal functions already in `src/medrag/ingest/standards.py` (`guess_doc_type`, `guess_specialty`) plus keyword/structure rules (dosing units + drug-name patterns → `DRUG_INFORMATION`; "must/should/contraindicated/is indicated when" → `RULE_CANDIDATE`; report-template-style repeated boilerplate phrasing → `REPORT_KNOWLEDGE`). Zero GPU cost, runs on the full pilot sample unconditionally.
2. **LLM tier** (`classifier: "llm_v1"`, opt-in) — reuses the existing MedicalRAG LLM client and its GPU discipline (`unload_local_before_generate`) to get a type + confidence + one-sentence rationale per chunk. Whether this tier runs for the pilot is a resourcing decision made at implementation time (target hardware is an 8GB-class GPU already shared between vLLM/embeddings/ASR) — heuristic-only results are still a valid, reportable pilot outcome; the LLM tier is there to raise precision if the heuristic-only eval isn't good enough, not a hard requirement to ship the pilot.

## 6. Evaluation gate — required before any scale-up decision

`artifact_pilot_eval.py` samples ~100 of the pilot's 1000 classified chunks, a human labels the "true" type, and the script computes per-type precision/recall against that gold set. Output: `reports/artifact_pilot_eval.md`, in the same PASS/FAIL-matrix style as `reports/full_system_verify.md`.

**No code in this pass scales classification beyond the ~1000-chunk pilot.** Going from 1k → 10k → 100k → 446k (the source feedback's own staged plan) is explicitly a future decision gated on this eval report, not something this implementation automates or defaults toward.

## 7. Closing the loop with the Rule Engine

Any pilot chunk classified `RULE_CANDIDATE` with confidence above a documented threshold is written into the relevant `backend/rules/banks/*.json` file as a **new rule entry with `status: "candidate"`** (never `approved`/`production` — see [`RULE_MODEL_SCHEMA_v1.md`](RULE_MODEL_SCHEMA_v1.md) §5). This is the only place the Knowledge Artifact pilot is allowed to write into the Rule Engine's data, and it never marks a rule as live on its own — a human still has to move it through validation.
