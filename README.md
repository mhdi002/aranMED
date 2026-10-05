# aranmed

Single-repo radiology stack: **ASR + institutional reporting + MedicalRAG** in one project root.

There is **no** top-level `asr/` or `medicalrag/` wrapper. Everything lives under this directory.

```
aranmed/
  backend/              # FastAPI ASR, report, EHR, alerts, education
                        #   + clinicaldb/ (MPI), pacs/, ehr/, interop/ (FHIR, HL7, CDA, EMS, transfers)
  frontend/             # Next.js UI
  src/medrag/           # MedicalRAG Python package
  scripts/              # ASR helpers + scripts/medrag/
  docs/                 # SYSTEM_OVERVIEW, MICROSERVICES, docs/medrag/
  deploy/               # Triton / deploy assets
  tests/                # ASR + tests/medrag/
  data/                 # MedRAG working data (local; gitignored corpora)
  qdrant_storage/       # Qdrant persistence (local; gitignored)
  config.yaml           # MedRAG defaults (paths via env overrides)
  docker-compose.yml    # Ollama + backend + frontend
  docker-compose.medrag.yml  # Qdrant + optional vLLM / embed profiles
  .env.example          # All hosts/ports/paths/models — copy to .env
  pyproject.toml        # pip install -e . → medrag-serve
  reports/              # Verification matrices (full_system_verify.*)
```

---

## Clinical platform: PACS · EHR · interoperability

Beside the radiology AI stack, AranMed is a complete clinical platform that hospitals connect to each other:

- **PACS** — DICOM archive with DIMSE (modalities, other PACS), DICOMweb, worklist/MPPS, a Cornerstone3D viewer and AI draft reads. See [`docs/core/PACS_v1.md`](docs/core/PACS_v1.md).
- **EHR + interop** — Master Patient Index, relational FHIR-aligned EHR linked to the PACS, FHIR R4 server, HL7 v2 (MLLP), C-CDA, EMS (NEMSIS), cross-hospital chart, transfers, consent and break-the-glass. See [`docs/core/EHR_INTEROP_v1.md`](docs/core/EHR_INTEROP_v1.md) and [`docs/core/CLINICAL_DB_SCHEMA_v1.md`](docs/core/CLINICAL_DB_SCHEMA_v1.md).

**User guide with screenshots of every page:** [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md). Complete illustrated guides with each section's screenshots and its test results: [English](docs/AranMed_Guide_EN.docx) · [فارسی (RTL)](docs/AranMed_Guide_FA.docx).

Tests:

```bash
pip install -r requirements-dev.txt
pytest                                   # unit + functional (real DICOM, HTTP, MLLP, two hospital processes)
DATABASE_URL=postgresql://… pytest --ignore=tests/medrag   # backend suite on Postgres
HOSPITAL_PG_DSN_BASE=postgresql://user@host:port pytest tests/functional/test_interhospital*.py   # hospitals on Postgres
HOSPITAL_PG_DSN_BASE=… HOSPITAL_PG_OIDS=2.25.702,2.25.602 pytest tests/functional/test_interhospital*.py   # mixed: listed hospitals on Postgres, others SQLite
cd frontend && npm run build && npm run test:e2e   # browser E2E against a seeded two-hospital stack
for l in en fa; do E2E_SCREENSHOTS=1 SCREENSHOT_LANG=$l npx playwright test screenshots; done  # docs/screenshots/<lang>/
node ../scripts/docs/build_docx.js   # rebuild the EN/FA DOCX guides from screenshots and docs/test-results/
```

## Architecture

Services discover each other **only through environment variables** (see `.env.example`). No committed absolute Windows paths.

```
Audio → ASR (hf_asr default | whisper-triton optional) → English transcript
      → template + Ollama → structured report
      → clinical_safety → critical_alerts + template_mismatch

Knowledge / explain / EHR-ask → HTTP MEDRAG_API_URL (/ask, /health)
MedRAG → Qdrant (QDRANT_URL) + embeddings (MEDRAG_EMBED_*) + LLM (MEDRAG_LLM_*)
```

| Service | Default | Env keys |
|---------|---------|----------|
| Frontend | `:3000` | `FRONTEND_PORT`, `BACKEND_URL` |
| Backend | `:8010` | `HOST`, `PORT`, `MEDRAG_API_URL`, `OLLAMA_*`, `TRITON_*` |
| MedRAG API | `:8080` | `MEDRAG_API_HOST`, `MEDRAG_API_PORT`, `MEDRAG_ROOT`, … |
| Embeddings (bge-m3) | `:8001` | `MEDRAG_EMBED_BASE_URL`, `MEDRAG_EMBED_MODEL`, `MEDRAG_EMBED_DEVICE` |
| vLLM LLM | `:8000` | `MEDRAG_LLM_BASE_URL`, `MEDRAG_LLM_MODEL`, `VLLM_GPU_MEM_UTIL` |
| Qdrant | `:6333` | `QDRANT_URL`, `MEDRAG_QDRANT_STORAGE` |
| Triton ASR | `:8002` | `TRITON_URL`, `TRITON_MODEL`, `WHISPER_DEVICE` |
| Ollama | `:11434` | `OLLAMA_HOST`, `OLLAMA_MODEL` |

### GPU placement (RTX 3070 8GB)

Prefer **CUDA for LLM + embeddings + ASR**. On 8GB:

| Workload | Device | Notes |
|----------|--------|-------|
| vLLM LLM | GPU | `VLLM_GPU_MEM_UTIL=0.55` (not 0.85) so embed fits |
| bge-m3 embed | GPU | `MEDRAG_EMBED_DEVICE=cuda` (Windows shim or vLLM embed) |
| Whisper ASR | GPU | `WHISPER_DEVICE=cuda` / `ASR_DEVICE=cuda` |
| Reranker CE | **CPU** | `MEDRAG_RERANK_DEVICE=cpu` — ~1–1.5GB; concurrent CUDA OOMs with LLM+embed |

Do not run LLM at 0.85 util and expect CUDA embeds — that leaves ~0.5GB free and pins bge-m3 on CPU (host CPU thrash).

**Whisper on 8GB:** `WHISPER_DEVICE=cuda` is the default in `.env`, but do **not** leave large Whisper resident together with vLLM + embed (OOMs). Start ASR when needed:

```powershell
pwsh -File scripts/start_triton_compat.ps1
```

Stop the compat process before heavy RAG/LLM sessions if VRAM is tight.

**Ollama:** keep as fallback only. Do not leave `qwen3.5-9b` loaded (`ollama ps`) while vLLM owns the GPU — a resident 9B can OOM/kill vLLM on 8GB.

---

## Quick start

```powershell
cd <this-repo>
Copy-Item .env.example .env
# Edit .env for your machine (URLs, optional corpus dirs). Never commit .env.

# Python (backend)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt   # if present; else install backend deps
pip install -e .                          # MedRAG package (src/medrag) → medrag-serve
# Or without install: $env:PYTHONPATH = "src"

# Frontend
cd frontend; npm install; cd ..
```

### RAG stack start order (RTX 3070 8GB)

Start **in this order** so VRAM and Qdrant are ready before `/ask`:

1. **Unload Ollama 9B** (if loaded) — `ollama ps` must be empty; a resident 9B OOMs vLLM+embed.
2. **Qdrant :6333** — `pwsh -File scripts/medrag/start_qdrant.ps1` (or `docker compose -f docker-compose.medrag.yml up -d qdrant`). Use the storage that holds your index (`MEDRAG_QDRANT_STORAGE`).
3. **vLLM LLM :8000** — WSL bitsandbytes at `VLLM_GPU_MEM_UTIL=0.55` (see `scripts/medrag/wsl/serve_qwen35_8gb.sh`). Wait until `GET http://127.0.0.1:8000/v1/models` returns 200.
4. **Embeddings :8001 CUDA** — `pwsh -File scripts/medrag/start_embed_windows.ps1` (needs free VRAM after util 0.55). Confirm `GET http://127.0.0.1:8001/health` → `device=cuda`.
5. **medrag-serve :8080** — from repo root with `.env` loaded: `$env:PYTHONPATH="src"; python -m medrag.interfaces.api`. Confirm `GET http://127.0.0.1:8080/health` → `llm.ok` + `embeddings.ok`.
6. **aranmed backend :8010** — keep `MEDRAG_TIMEOUT_SEC≥900` (1200 recommended) so `/api/knowledge/ask` and `/api/chat` do not abort mid-RAG. Then frontend :3000.

Cold first `/ask` can take **5–15+ minutes** (CPU rerank + retrieval + vLLM). Do not lower timeouts below that for live verify.

### Run MedRAG dependencies

```powershell
# Qdrant (persists under ./qdrant_storage by default)
docker compose -f docker-compose.medrag.yml up -d qdrant
# Or: pwsh -File scripts/medrag/start_qdrant.ps1

# Optional GPU LLM + embeddings (profiles)
# docker compose -f docker-compose.medrag.yml --profile vllm up -d
# docker compose -f docker-compose.medrag.yml --profile vllm-embed up -d

# Bare metal / WSL (preferred on this workstation):
#   WSL vLLM :8000  →  scripts/medrag/wsl/serve_qwen35_8gb.sh
#   Windows embed :8001 →  pwsh -File scripts/medrag/start_embed_windows.ps1
```

### Run MedRAG API

```powershell
$env:MEDRAG_ROOT = "."
$env:PYTHONPATH = "src"
# medrag-serve
python -m medrag.interfaces.api
# Health: GET http://127.0.0.1:8080/health
# Ask:    POST http://127.0.0.1:8080/ask  {"query":"..."}  (allow 15–20 min cold)
```

### Run aranmed backend + frontend

```powershell
# Backend (from repo root, with .venv active) — MEDRAG_TIMEOUT_SEC from .env
$env:PYTHONPATH = "backend"
uvicorn app:app --host 0.0.0.0 --port 8010 --app-dir backend
# Or: .\scripts\start_backend.ps1

# Frontend
$env:BACKEND_URL = "http://127.0.0.1:8010"
cd frontend; npm run dev
```

Docker all-in-one (Ollama + backend + frontend; MedRAG/Triton still external URLs):

```powershell
docker compose up -d --build
```

---

## MedRAG data & Qdrant folders

| Folder | Purpose | Env |
|--------|---------|-----|
| `./data` | OCR text, chunks, manifests, archives | `MEDRAG_DATA_DIR=./data` |
| `./qdrant_storage` | Live Qdrant disk persistence | `MEDRAG_QDRANT_STORAGE=./qdrant_storage` |
| `./catalog.db` | Title/specialty catalog (gitignored) | `MEDRAG_CATALOG_DB=./catalog.db` |

Both keep `.gitkeep` + a short README; **corpora and indexes are gitignored**.

### Migrating an existing MedicalRAG install

Do **not** bake old Desktop paths into code. In local `.env` either:

1. Point at the old folders:
   ```env
   MEDRAG_DATA_DIR=<path-to-old-data>
   MEDRAG_QDRANT_STORAGE=<path-to-old-qdrant_storage>
   MEDRAG_CATALOG_DB=<path-to-old-catalog.db>
   ```
2. Or copy/junction into this repo (one-time; replace `SOURCE` yourself):
   ```powershell
   # robocopy "SOURCE\data" ".\data" /E
   # robocopy "SOURCE\qdrant_storage" ".\qdrant_storage" /E
   # Copy-Item "SOURCE\catalog.db" ".\catalog.db"
   ```

Corpus libraries (Mehrsys, standards, textbooks) similarly use `MEDRAG_MEHRSYS_DIR`, `MEDRAG_STANDARDS_DIR`, `MEDRAG_LIBRARY_DIR`, `MEDRAG_EXAM_DIR` in `.env` only.

---

## Templates, report rules, alerts

- **Templates** (~56 institutional): `backend/data/templates/` — `GET /api/templates`
- **Naming rules**: `backend/data/report_rules/` + `REPORT_RULES.json` — used by `/api/report`
- **Critical alerts / template_mismatch**: returned on `POST /api/report`; unit tests in `tests/test_clinical_safety.py`
- Env overrides: `TEMPLATES_DIR`, `REPORT_RULES_*`, `CLINICAL_SAFETY_*` (see `.env.example`)

---

## Verification

```powershell
# Bring up separate processes first (same as pre-merge): backend :8010, medrag-serve :8080,
# Qdrant :6333, embed :8001, vLLM :8000, Ollama :11434, frontend :3000.
# Long MedRAG asks need a high client/backend timeout:
$env:MEDRAG_TIMEOUT_SEC = "1200"
.\.venv\Scripts\python.exe scripts\full_system_verify.py
# Writes reports/full_system_verify.md and .json
# Optional: $env:SKIP_ASR = "1"  # reuse prior ASR PASS when only retesting RAG/EHR
```

Covers health, auth, templates, English ASR (triton + hf_asr), full-audio coverage, report fields, MedRAG knowledge ask, EHR build (Ollama), clinical_safety pytest, code integrity. Extended embed/RAG checks are included when services are up; otherwise marked **BLOCKED** with reason.

---

## Gitignore notes

Ignored (among others): `.env`, `.venv`, `node_modules`, `.next`, model weights, audio fixtures, **`data/**` corpora**, **`qdrant_storage/**` indexes**, `catalog.db`, caches. Structure kept via `.gitkeep` / folder READMEs.

---

## Install MedRAG package

```powershell
pip install -e .
# entry points: medrag-serve, medrag-query, medrag-pipeline
# or: $env:PYTHONPATH="src"; python -c "import medrag"
```

---

## License / PHI

Treat uploads, transcripts, and local DBs as PHI. Do not commit secrets, `.env`, or vector stores.
