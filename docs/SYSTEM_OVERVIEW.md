# aranmed — System Overview

End-to-end reference for engineers and operators. Companion docs:

- Microservice topology & env keys: [`MICROSERVICES.md`](MICROSERVICES.md)
- Project setup / API / troubleshooting: [`../README.md`](../README.md)
- Latest verification matrix: [`../reports/full_system_verify.md`](../reports/full_system_verify.md)

MedicalRAG lives in this monorepo as `src/medrag` but runs as a **separate HTTP microservice**. The ASR backend talks to it only over HTTP (`MEDRAG_API_URL`) — no in-process imports of `medrag` from `backend/`.

---

## 1. What aranmed is

**aranmed** is a bilingual radiology reporting stack:

1. Capture or upload dictation audio (Persian, English, or code-switched).
2. Transcribe with Whisper (`hf_asr` by default, optional NVIDIA Triton).
3. Produce an **English clinical transcript** (required for downstream LLM report structuring).
4. Fill an **institutional report template** via Ollama (structured report).
5. Surface **critical / lethal finding alerts** and **template-mismatch** flags.
6. Answer radiology **knowledge** questions via MedicalRAG (when that service is up).

Additional modules (role-gated): EHR build/Q&A, medication due alerts (email/SMS), and education (MCQ / case / exam / explain).

UI languages: English and Persian (RTL). Clinical report text for LLM structuring is English.

```
Audio → ASR (hf_asr | whisper-triton) → English transcript
      → template + Ollama → structured report
      → clinical_safety → critical_alerts + template_mismatch

Text knowledge / explain / EHR-ask → MedicalRAG HTTP /ask  (external)
```

---

## 2. Architecture / microservices

Services discover each other through **environment variables**. Application code must not hardcode hosts, ports, or machine-specific paths.

| Service | Default port | Role |
|---------|--------------|------|
| Frontend (Next.js) | **3000** | Recorder, upload, template picker, role workspaces; proxies `/api/*` → backend |
| aranmed backend (FastAPI) | **8010** | Auth, ASR orchestration, report, chat, EHR, alerts, education |
| MedicalRAG (`src/medrag`) | **8080** | Knowledge Q&A over HTTP `/ask`, `/ask/image`, `/health` |
| Triton Inference Server (optional) | **8002** (host → container 8000) | Additive Whisper ASR provider `whisper-triton` |
| Ollama | **11434** | Core LLM for report structuring, EHR build, MCQ/case/exam |
| Qdrant + embed/LLM | **6333** / **8001** / **8000** | `docker-compose.medrag.yml` + `MEDRAG_*` / `QDRANT_*` env |

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
     │ MEDRAG_API_URL │          │ OLLAMA_HOST      │          │ TRITON_URL     │
     │ :8080          │          │ :11434           │          │ :8002 optional │
     └────────┬───────┘          └──────────────────┘          └────────────────┘
              │
              ▼
     Qdrant + vLLM (MedicalRAG project — black box)
```

**Rules**

- Point services with `BACKEND_URL`, `MEDRAG_API_URL` / `MEDICALRAG_URL`, `OLLAMA_HOST`, `TRITON_URL`, `TEMPLATES_DIR`, `REPORT_RULES_*`, `MEDRAG_ROOT` (scripts only).
- Default ASR remains local **`whisper-large-v3` / `hf_asr`**. Triton is additive (`whisper-triton`, `default: false` in `backend/models.yaml`).
- MedicalRAG client: `backend/integrations/medrag_client.py` (httpx only).

---

## 3. Auth and roles

- Store: SQLite `backend/data/app.db` (auto-created).
- Passwords: scrypt. Sessions: bearer tokens (`ASR_AGENT_SECRET`, TTL via `ASR_AGENT_TOKEN_TTL`).
- Default first-boot admin: `admin` / `admin` (change immediately).
- Roles: `radiologist`, `doctor`, `resident`, `student`, `admin`.

Workspace views (`frontend/lib/roles.js`):

| Role | Views |
|------|--------|
| **radiologist** | `dictate`, `radiology` (dictation + radiology knowledge chat only) |
| doctor | dictate, radiology, reports, templates, ehr, alerts, settings |
| resident | dictate, radiology, reports, templates, ehr, education, settings |
| student | education, settings |
| admin | all of the above plus asr / llm / vision admin views |

Public without token: `/api/health`, `/api/templates`, `/api/auth/register`, `/api/auth/login`. Most other routes require `Authorization: Bearer …`.

---

## 4. Full dictation pipeline

Typical UI path (dictate workspace):

1. **Capture / upload** audio (`.m4a`, `.wav`, `.mp3`, …) — ffmpeg required for many formats.
2. **ASR** — `POST /api/transcribe` (or one-shot `POST /api/dictate`):
   - Default model: `whisper-large-v3` (`provider: hf_asr`).
   - Optional: `model=whisper-triton` when Triton is configured and healthy.
3. **English clinical text** — ASR is configured with `output_english: true` (Whisper translate path). If Persian script remains, `backend/english_transcript.py` can ask the core LLM to produce English-only dictation.
4. **Template selection** — user picks an institutional template id from `GET /api/templates` (~56 entries under `backend/data/templates/*.txt`).
5. **Structured report** — `POST /api/report` sends transcript + template to Ollama with a structure system prompt. Selected template **always wins** for headings / exam title.
6. **Safety enrichment** — `clinical_safety.enrich_report_payload` attaches:
   - `critical_alerts` — local pattern triage (+ optional MedRAG confirmation).
   - `template_mismatch` — spoken exam name vs UI-selected template.
7. **UI banners** — `CriticalAlertsBanner` and `TemplateMismatchBanner` render those fields.

One-shot: `POST /api/dictate` = transcribe (default ASR) then `report`.

---

## 5. English transcript requirement

Downstream report structuring expects **English** clinical dictation.

| Layer | Behavior |
|-------|----------|
| `hf_asr` | `output_english: true` → Whisper `task=translate` for English output |
| `whisper-triton` | Server/env `WHISPER_OUTPUT_ENGLISH` / client `output_english: true` |
| Fallback | `to_english_clinical()` if Persian script (`\u0600–\u06FF`) remains |

Do not feed Persian-script transcripts into `/api/report` in production flows; the pipeline is designed to normalize to English first.

---

## 6. Full-audio transcription (no mid-file truncation)

Long dictations (> ~`WHISPER_MAX_SHORTFORM_S`, default 30 s) must cover the **entire file**, not only the first window.

**`hf_asr`** (`backend/providers/hf_asr.py`):

- Short clips: single-pass generate.
- Long clips: `_generate_long_form` — consecutive windows of `WHISPER_CHUNK_LENGTH_S` with optional `WHISPER_STRIDE_LENGTH_S` overlap, joined end-to-end.
- Avoids Whisper’s internal long-form seek stopping mid-file on mixed FA/EN dictation.

**Triton path** (compat HTTP / Python backend under `deploy/triton/`):

- Same env knobs for short-form threshold and chunking so host Triton also covers full files.

Verification of start+end anchors on longer Desktop samples is recorded in `reports/truncation_fix_verify.*` and the full matrix in `reports/full_system_verify.*`.

---

## 7. Templates and insurance naming rules

### Institutional templates

- Location: `backend/data/templates/*.txt` (filename stem = `template_id`).
- Current pack size: **56** templates (abdominal, CDS, OB, thyroid, etc.).
- Import: `scripts/import_report_templates.py --source "<rtf/docx folder>"`.
- Optional radreport.org scrape is **off** by default (`TEMPLATES_FETCH_RADREPORT=0`) so institutional titles win.

### Report / insurance naming rules

- Files under `backend/data/report_rules/` (DOCX / TXT / PDF) plus `backend/data/REPORT_RULES.json`.
- Env: `REPORT_RULES_DIR`, `REPORT_RULES_DOCX`, `REPORT_RULES_TXT`, `REPORT_RULES_PDF`, `REPORT_RULES_PATH`, `REPORT_RULES_MAX_CHARS`, `REPORT_RULES_MEDRAG`.
- During `/api/report`, optional MedRAG consult for naming (`consult_medrag_naming_rules`) when `REPORT_RULES_MEDRAG=1` and MedRAG is reachable.
- Embed rules into MedicalRAG/Qdrant (operator script, needs `MEDRAG_ROOT` / `MEDRAG_LIBRARY_DIR`):  
  `scripts/ingest_report_rules_medrag.py`

Never hardcode Downloads paths in app code — copy rules into `backend/data/report_rules/` and point env vars at those relative paths.

---

## 8. MedicalRAG (external HTTP service)

Adapter: `backend/integrations/medrag_client.py`.

| Endpoint | Use |
|----------|-----|
| `GET /health` | Liveness (surfaced in `GET /api/health` → `medrag`) |
| `POST /ask` | Text knowledge (`specialty` e.g. `radiology`) |
| `POST /ask/image` | Image/OCR-style ask (path + query) |

**Uses MedicalRAG**

- Radiology text knowledge chat (`POST /api/chat` text-only → MedRAG).
- Education **explain** (MedRAG with Ollama fallback depending on tool wiring).
- EHR patient Q&A (`POST /api/ehr/{id}/ask`).
- Optional report-rules grounding / clinical-safety confirmation when env flags enable it.

**Does *not* use MedicalRAG** (stays on Ollama / local rules)

- Structured report fill (`/api/report`, `/api/dictate`).
- Structured EHR **build**.
- Education MCQ / case / exam generators.
- Medication due-date alerts (rule-based).

If MedRAG is down, knowledge chat returns an error stating MedicalRAG is unavailable; structured reporting can still run if Ollama + ASR are up.

---

## 9. Critical / lethal finding alerts

Module: `backend/clinical_safety.py`.

1. **Local heuristics** always run (regex patterns for tension pneumothorax, aortic rupture/dissection, massive PE, ruptured ectopic, etc., including some Persian phrases).
2. **Optional MedRAG** triage when `CLINICAL_SAFETY_USE_MEDRAG` / `CLINICAL_SAFETY_ALWAYS_MEDRAG` / `CLINICAL_SAFETY_CONFIRM_MEDRAG` are set.
3. API responses include `critical_alerts: [{ code, severity, message, source, … }, …]`.
4. Frontend shows a prominent **CriticalAlertsBanner**.
5. If `patient_id` + owner user are present, alerts may be logged on the EHR alert channel (`channel="clinical"`, dry-run style log via `store.log_alert`).

Always treat generated reports as **draft** — human review before signing.

---

## 10. Template mismatch

When the dictation *names* a different exam/template than the one selected in the UI:

- `assess_template_mismatch(transcript, selected_template_id)` compares spoken title vs catalog / official insurance titles.
- **`selected_wins: true`** — report structure and EXAM title follow the **UI-selected** template.
- Response field `template_mismatch` includes `mismatch`, `spoken_template`, `matched_template_id`, `naming_violations`, and a human-readable `message`.
- Structure system prompt (`STRUCTURE_SYS_EXTRA`) instructs the LLM not to switch templates.
- UI: **TemplateMismatchBanner**.

Example: doctor says “brain sonography” while `chest` is selected → structured chest report + mismatch flag.

---

## 11. EHR, medication alerts, education (brief)

| Module | Behavior |
|--------|----------|
| **EHR** | `POST /api/ehr/build` — free-form patient info → structured record via **Ollama**. Per-user SQLite storage. `POST /api/ehr/{id}/ask` — Q&A via **MedicalRAG**. |
| **Alerts** | `POST /api/alerts/check` / `send` — medications due from EHR; Email (SMTP) or SMS (Twilio); **dry-run** if credentials unset. Separate from clinical finding alerts. |
| **Education** | MCQ / case / exam via **Ollama**; explain prefers **MedicalRAG**. Saved quizzes in SQLite. |

---

## 12. Configuration / environment reference

Copy `.env.example` → `.env`. Full comments live there; key groups:

| Group | Variables |
|-------|-----------|
| Backend listen | `HOST`, `PORT` (8010) |
| Frontend | `FRONTEND_PORT`, `BACKEND_URL` |
| Ollama | `OLLAMA_HOST`, `OLLAMA_MODEL` |
| MedicalRAG | `MEDRAG_API_URL` (alias `MEDICALRAG_URL`), `MEDRAG_TIMEOUT_SEC`, script-only `MEDRAG_ROOT`, `MEDRAG_LIBRARY_DIR`, `MEDRAG_PYTHON` |
| Templates / rules | `TEMPLATES_DIR`, `TEMPLATES_FETCH_RADREPORT`, `REPORT_RULES_*`, `REPORT_RULES_MEDRAG` |
| Clinical safety | `CLINICAL_SAFETY_USE_MEDRAG`, `CLINICAL_SAFETY_ALWAYS_MEDRAG`, `CLINICAL_SAFETY_CONFIRM_MEDRAG` |
| Triton (optional) | `TRITON_URL`, `TRITON_MODEL`, `TRITON_PROTOCOL`, `TRITON_TIMEOUT_SEC` |
| Whisper long-form | `WHISPER_MAX_SHORTFORM_S`, `WHISPER_CHUNK_LENGTH_S`, `WHISPER_STRIDE_LENGTH_S`, `WHISPER_TARGET_SR`, `WHISPER_OUTPUT_ENGLISH`, `WHISPER_TASK`, `WHISPER_DEVICE` |
| Local ASR | `HF_HOME`, `ASR_DEVICE`, `ASR_DTYPE` |
| Auth / notify | `ASR_AGENT_SECRET` / JWT, SMTP_*, TWILIO_* |
| Optional providers | `VIBEVOICE_MODEL_PATH`, `CRISPASR_*`, `VISION_MODEL_PATH`, … |

Model registry: `backend/models.yaml` (local) / `backend/models.docker.yaml` (Compose).

---

## 13. How to run / deploy checklist

### Windows (local)

1. Ollama running; model matching `OLLAMA_MODEL` / `models.yaml` pulled.
2. Optional: MedicalRAG stack up → `MEDRAG_API_URL=http://127.0.0.1:8080`.
3. Optional: Triton on host **8002** → `TRITON_URL=http://127.0.0.1:8002`, `whisper-triton` enabled in `models.yaml`.
4. Backend (Python **with PyTorch** — prefer conda / `scripts/start_backend.ps1`):
   ```powershell
   .\scripts\start_backend.ps1
   ```
5. Frontend:
   ```powershell
   .\scripts\start_frontend.ps1
   ```
6. Open `http://127.0.0.1:3000`. Health: `http://127.0.0.1:8010/api/health`.

### Linux GPU server

```bash
cp .env.example .env   # edit URLs / models
make install
make run               # :8010 + :3000
```

### Docker Compose

`make docker-install` / `make docker-run` — Ollama + backend + frontend. Point `MEDRAG_API_URL` / `TRITON_URL` at reachable hosts on your mesh.

### Operator checklist

- [ ] `.env` set; no secrets committed
- [ ] `GET /api/health` → `ok: true`, Ollama available
- [ ] MedRAG health OK if knowledge features needed
- [ ] `GET /api/templates` → ~56 institutional templates
- [ ] Short English ASR smoke (`has_persian: false`)
- [ ] Long-file ASR shows start **and** end content
- [ ] `POST /api/report` returns `report`, `critical_alerts`, `template_mismatch`
- [ ] `pytest tests/test_clinical_safety.py` passes

---

## 14. What was verified

Automated matrix (this checkout) — see [`reports/full_system_verify.md`](../reports/full_system_verify.md) and [`.json`](../reports/full_system_verify.json). Runner: `scripts/full_system_verify.py`.

Latest recorded run (UTC finish `2026-07-22T18:54:40`): **8 PASS / 0 FAIL / 1 BLOCKED**.

| Check | Result |
|-------|--------|
| Health `GET /api/health` | PASS (backend up; MedRAG probe failed) |
| Auth `admin` / `admin` | PASS |
| Templates | PASS (count=56) |
| English ASR (`5.12` m4a, triton + hf_asr) | PASS (`has_persian: false`) |
| Full-audio coverage (`5.15` m4a, start+end) | PASS |
| Report fields (`report`, `critical_alerts`, `template_mismatch`) | PASS |
| Knowledge ask | **BLOCKED** (MedicalRAG :8080 down) |
| `tests/test_clinical_safety.py` | PASS (7 tests) |
| Code integrity (hf_asr / Triton additive / MedRAG HTTP-only) | PASS |

Services during that run: backend **UP**, Triton **UP**, frontend **DOWN**, MedRAG **DOWN**.

Re-run:

```powershell
.\.venv\Scripts\python.exe scripts\full_system_verify.py
```

---

## Microservice discipline (summary)

- **No hardcoding** of hosts, ports, or absolute Downloads paths in application code.
- **MedicalRAG** = separate product; aranmed is a client only.
- **Triton** = optional ASR scale-out; default remains `hf_asr`.
- Prefer env + `models.yaml` toggles over code edits when moving between machines.
