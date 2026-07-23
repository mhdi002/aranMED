# aranmed microservice topology

Services talk only via env-configured URLs. Do not bake hosts into application code.

```
┌─────────────┐   relative /api    ┌──────────────────┐
│  frontend   │ ─────────────────▶ │ aranmed-backend  │
│  :3000      │   BACKEND_URL      │ :8010            │
└─────────────┘                    └────────┬─────────┘
                                            │
              ┌─────────────────────────────┼─────────────────────────────┐
              │                             │                             │
              ▼                             ▼                             ▼
     ┌────────────────┐          ┌──────────────────┐          ┌────────────────┐
     │ MedicalRAG     │          │ Ollama / LLM     │          │ Triton ASR     │
     │ src/medrag     │          │ OLLAMA_HOST      │          │ TRITON_URL     │
     │ MEDRAG_API_URL │          │ default :11434   │          │ (optional)     │
     │ default :8080  │          │                  │          │                │
     └────────┬───────┘          └──────────────────┘          └────────────────┘
              │
              ▼
     Qdrant + embed/LLM (docker-compose.medrag.yml / MEDRAG_* env)
```

## Environment keys

| Service | Key | Purpose |
|---------|-----|---------|
| Frontend → backend | `BACKEND_URL` | Proxy target for `/api/*` |
| Backend → MedicalRAG | `MEDRAG_API_URL` | HTTP `/ask`, `/ask/image`, `/health` |
| Backend → Ollama | `OLLAMA_HOST`, `OLLAMA_MODEL` | Structured LLM tasks |
| Backend → Triton | `TRITON_URL`, `TRITON_MODEL` | Optional ASR provider |
| Whisper cache | `HF_HOME`, `ASR_DEVICE` | Local `hf_asr` — use `ASR_DEVICE=cuda` |
| Whisper / Triton | `WHISPER_DEVICE` | Compat server — use `cuda` (not `cpu`) |
| Report templates | `TEMPLATES_DIR` | Institutional templates under `backend/data/templates` |
| Naming rules | `REPORT_RULES_DOCX` / `REPORT_RULES_PATH` | Insurance exam titles (local + MedRAG) |
| MedRAG root / data | `MEDRAG_ROOT`, `MEDRAG_DATA_DIR` | Defaults: `.` and `./data` |
| Qdrant | `QDRANT_URL`, `MEDRAG_QDRANT_STORAGE` | Defaults: `:6333` and `./qdrant_storage` |
| MedRAG embed / LLM | `MEDRAG_EMBED_*`, `MEDRAG_LLM_*`, `VLLM_GPU_MEM_UTIL` | bge-m3 / vLLM; embed `MEDRAG_EMBED_DEVICE=cuda` |
| MedRAG rerank | `MEDRAG_RERANK_DEVICE` | Keep `cpu` on 8GB with LLM+embed resident |
| Corpus dirs (local `.env`) | `MEDRAG_LIBRARY_DIR`, `MEDRAG_*_DIR` | Never commit machine paths |

See root `.env.example` for a full template. Report-title rules DOCX/PDF live under
`backend/data/report_rules/` (never hardcode Downloads paths).

## Start order (local)

```bash
# From this monorepo root (set MEDRAG_ROOT=. in .env)
cp .env.example .env                  # edit URLs; no absolute paths in git

# 1) MedRAG deps + API (package: src/medrag)
docker compose -f docker-compose.medrag.yml up -d qdrant
# optional: --profile vllm / --profile vllm-embed
export PYTHONPATH=src                 # or: pip install -e .
medrag-serve                          # or: python -m medrag.interfaces.api
# health: GET $MEDRAG_API_URL/health

# 2) Ollama (structured EHR / MCQ / reports)
ollama serve

# 3) aranmed backend + frontend
uvicorn app:app --host "$HOST" --port "$PORT" --app-dir backend
cd frontend && BACKEND_URL=http://127.0.0.1:8010 npm run dev
```

## Triton ASR (additive — Whisper `hf_asr` stays default)

```bash
# Prepare Python-backend Whisper (WAV→TRANSCRIPT). Stock ONNX is not e2e-compatible:
#   .venv/Scripts/python.exe scripts/export_whisper_triton.py
#
# Run Triton on host :8002 (container HTTP :8000). Avoid host :8001 (MedRAG embed).
docker run --gpus all --rm -p 8002:8000 \
  -v "$(pwd)/deploy/triton/model_repository:/models" \
  -v "$(pwd)/models/whisper-large-v3:/whisper-weights:ro" \
  -e WHISPER_MODEL_DIR=/whisper-weights \
  nvcr.io/nvidia/tritonserver:24.08-py3 \
  tritonserver --model-repository=/models

# Then in .env:
#   TRITON_URL=http://127.0.0.1:8002
#   TRITON_MODEL=whisper
# In models.yaml: whisper-triton enabled: true; keep hf_asr as default (or set default)
```

Local Whisper (`provider: hf_asr`, `whisper-large-v3`) remains the default and is **not** modified by the Triton client.

## docker-compose note

`docker-compose.yml` already runs `ollama` + `backend` + `frontend` with `OLLAMA_HOST=http://ollama:11434` and `BACKEND_URL=http://backend:8010`. Point `MEDRAG_API_URL` / `TRITON_URL` at reachable service names on your mesh (or host gateway) without code changes.

## What uses which brain

| Feature | Service |
|---------|---------|
| Dictation ASR | Triton (optional) or local Whisper `hf_asr` |
| Report structuring / dictate | Ollama (core) |
| EHR structured build | Ollama |
| EHR Q&A about patient | MedicalRAG (`POST /api/ehr/{id}/ask`) |
| Education MCQ / case / exam | Ollama |
| Education explain | MedicalRAG (fallback Ollama) |
| Radiology text knowledge | MedicalRAG `specialty=radiology` |
| Radiology pixel VL | Local vision provider |
| Medication alerts | Rule-based (no LLM) |
