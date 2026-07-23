# aranmed full system verification

- Started (UTC): `2026-07-22T22:55:52.932285+00:00`
- Finished (UTC): `2026-07-22T23:56:56Z` (MedRAG ask finalize)
- API base: `http://127.0.0.1:8010`
- **SKIP_ASR: no** (earlier live English + full-audio ASR retained)
- Source: original `scripts/full_system_verify.py` + focused retest `reports/_retest_medrag_asks.py` + live `POST /api/knowledge/ask`
- Config fixes (no `src/medrag` / Whisper logic changes): `MEDRAG_TIMEOUT_SEC=1200`, `MEDRAG_LLM_TIMEOUT=900`, `MEDRAG_EMBED_TIMEOUT=180`, `MEDRAG_LLM_ENABLE_THINKING=0`; Ollama 9B kept unloaded during RAG

## Root cause (prior FAIL)

| Factor | What happened |
|--------|----------------|
| Backend proxy timeout | Live `.env` had `MEDRAG_TIMEOUT_SEC=120` while cold `/ask` needs ~3–8+ min (retrieve + CPU rerank + vLLM) |
| Mid-ask crash | When vLLM was down/unreachable, MedRAG fell back to Ollama 9B on 8GB → OOM / WinError 10054 connection reset |
| Warm-up latency | First clinical ask ~476s (CPU reranker + retrieval); subsequent asks faster once warm |
| Concurrent Ollama | Leaving `qwen3.5-9b` loaded fights vLLM+embed for VRAM |

## Services (end-of-finalize probes)

| Service | Status |
|---------|--------|
| Backend :8010 | UP (`MEDRAG_TIMEOUT_SEC=1200`) |
| Frontend :3000 | UP |
| MedicalRAG :8080 | UP — health `llm.ok` + `embeddings.ok`, chunks=446576 |
| Triton :8002 | UP |
| Embeddings :8001 | UP (`BAAI/bge-m3`, CUDA) |
| Qdrant :6333 | UP (collections green; expand ~441k points) |
| vLLM :8000 | UP (`Qwen/Qwen3.5-4B` bitsandbytes, `VLLM_GPU_MEM_UTIL=0.55`) |
| Ollama :11434 | UP but **no model loaded** (`/api/ps` → `[]`) |

## Matrix

| # | Check | Result | Notes |
|---|-------|--------|-------|
| 1 | `health` | **PASS** | GET /api/health |
| 2 | `auth_login` | **PASS** | admin/admin |
| 3 | `templates` | **PASS** | count=56 |
| 4 | `english_asr` | **PASS** | Feb 3*5.12*: whisper-triton + hf_asr |
| 5 | `full_audio_coverage` | **PASS** | Feb 3*5.15* start/end anchors |
| 6 | `report` | **PASS** | hepatobiliary + critical_alerts + template_mismatch |
| 7 | `embeddings_bge_m3` | **PASS** | embed :8001 http=200 |
| 8 | `medrag_ask_suite` | **PASS** | clinical 200/695ch/476.5s; radiology 200/1088ch/418.3s; drug 200/373ch/173.8s; chunks=446576 |
| 9 | `report_rules_files` | **PASS** | DOCX/TXT/PDF + REPORT_RULES.json |
| 10 | `knowledge_ask` | **PASS** | `/api/chat` 200 in 310.8s; also live `POST /api/knowledge/ask` 200 in 264.3s, answer 1080 chars, **4 sources** (e.g. Ma Mateers Emergency Ultrasound p.342) |
| 11 | `ehr_build` | **PASS** | http=200 |
| 12 | `medrag_import` | **PASS** | `src/medrag` |
| 13 | `clinical_safety_pytest` | **PASS** | 7 passed (prior) |
| 14 | `code_integrity` | **PASS** | hf_asr intact; Triton additive; MedRAG HTTP-only |

**Totals:** PASS=14 FAIL=0 BLOCKED=0

## Direct answers (finalize)

| Question | Verdict | Evidence |
|----------|---------|----------|
| MedRAG `/ask` working? | **YES** | Three clinical/radiology/drug asks → non-empty answers; health llm+embed OK |
| aranmed knowledge ask working? | **YES** | Auth + `POST /api/knowledge/ask` → answer + sources in `raw.sources` |
| vLLM working? | **YES** | `/v1/models` 200; chat completion `PONG`/`OK`; MedRAG uses it for generate |
| Whisper English ASR? | **YES** | Prior live PASS retained |
| Triton? | **YES** | Prior live PASS + :8002 ready |

## Evidence snippets

**MedRAG health:** `chunks=446576`, `llm.ok=true` (vLLM Qwen3.5-4B), `embeddings.ok=true` (bge-m3 :8001).

**Clinical `/ask` preview:** signs of acute appendicitis (pain, nausea, vomiting, anorexia, fever) with retrieved context.

**`/api/knowledge/ask`:** ultrasound appendicitis criteria (≥6 mm noncompressible blind-ending tube); 4 sources; ~264s.

See `reports/full_system_verify.json` and `reports/_knowledge_ask_live.json` for payloads.
