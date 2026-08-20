# AranMed Configuration Reference

Companion docs: [`CORE_ARCHITECTURE_v1.md`](CORE_ARCHITECTURE_v1.md) · [`DEPLOYMENT.md`](DEPLOYMENT.md) · [`../SYSTEM_OVERVIEW.md`](../SYSTEM_OVERVIEW.md)

## 1. The no-hardcoding rule

Every host, port, path, model name, timeout, threshold and scaling parameter in AranMed is supplied by configuration. The codebase follows one rule:

> **Application code never contains a deployment literal.** A literal may appear only as a *documented default* in a designated configuration module, and that default must be overridable by an environment variable.

Concretely, the designated configuration surfaces are:

| Layer | Configuration surface | Notes |
|---|---|---|
| Backend | `backend/config.py` | Reads `.env` (repo root, then `backend/`). Every constant is `os.getenv(...)` with a default. |
| Backend model registry | `backend/models.yaml` (path via `ASR_AGENT_REGISTRY_YAML`) | Declares which model serves each role (`asr`, `core`, `vision`), the provider, and defaults. |
| Rule Engine | `backend/rules/banks/*.json` | Rule *content* is data. `engine.py` / `repository.py` contain no `rule_id`, message or pattern. See [`RULE_MODEL_SCHEMA_v1.md`](RULE_MODEL_SCHEMA_v1.md) §6. |
| MedicalRAG | `config.yaml` + `src/medrag/config.py` | `_env()` overrides every YAML value. `QDRANT_URL` has **no** literal fallback — it raises if unset. |
| Frontend | `lib/config.js`, runtime `GET /api/health` + `GET /api/models` | No model name or endpoint is written into a component; the registry is the source of truth. |
| Ops scripts | `scripts/_endpoints.py` | Single resolver for all service URLs; scripts import from it instead of embedding hosts. |
| Deployment | `docker-compose.yml` + `.env` | Every value is `${VAR:-default}`. The nginx config is a template rendered from env at container start. |

### What changed to enforce this

- `frontend/components/views/ModelsView.js` previously printed `"OmniASR-LLM-7B"` / `"Qwen LLM"`; it now renders whatever the registry reports, including provider, warm state and VRAM.
- `frontend/components/views/RadiologyView.js` previously printed `"Radiology-Infer-Mini Q8"` and `"llama-server :8088"`; it now reads `vision_model` and MedicalRAG status from `/api/health`.
- `frontend/lib/viewMeta.js` titles are role-generic (`Speech Recognition Model`) instead of naming a specific model.
- `src/medrag/index/vectorstore.py` had `STORE_NAMES = ("main","standards","expand","expand2","expand3")` hardcoded; it now comes from `MEDRAG_QDRANT_STORES` so a corpus that never created `expand2/3` is not probed for them.
- `src/medrag/index/vectorstore.py::require_server` no longer falls back to a literal `http://localhost:6333`.
- Verification scripts (`full_system_verify.py`, the clinical-safety e2e scripts, `test_rag_and_triton_samples.py`) now import `scripts/_endpoints.py`.

Verify at any time:

```bash
python scripts/_endpoints.py          # what every script will target
docker compose config                 # fully-resolved deployment values
```

## 2. Service endpoints

Resolved by `scripts/_endpoints.py`; first non-empty wins.

| Service | Environment variables | Default |
|---|---|---|
| Backend | `ASR_API_BASE`, `BACKEND_URL`, `ARANMED_BACKEND_URL` | `http://127.0.0.1:8010` |
| Frontend | `FRONTEND_URL`, or `FRONTEND_HOST` + `FRONTEND_PORT` | `http://127.0.0.1:3000` |
| Gateway (public) | `GATEWAY_URL`, `PUBLIC_URL` | falls back to frontend |
| MedicalRAG | `MEDRAG_API_URL`, `MEDICALRAG_URL` | `http://127.0.0.1:8080` |
| Qdrant | `QDRANT_URL` | `http://127.0.0.1:6333` |
| Ollama | `OLLAMA_HOST` | `http://127.0.0.1:11434` |
| vLLM generation | `MEDRAG_LLM_BASE_URL`, `VLLM_BASE_URL` | `http://127.0.0.1:8000/v1` |
| vLLM embeddings | `MEDRAG_EMBED_BASE_URL` | `http://127.0.0.1:8001/v1` |
| Triton (optional ASR) | `TRITON_URL` | `http://127.0.0.1:8002` |

## 3. Knowledge corpus

The knowledge base is **not** bundled or mocked — point the stack at a real MedicalRAG corpus.

| Variable | Meaning |
|---|---|
| `MEDRAG_QDRANT_STORAGE` | Host path to the Qdrant storage directory (mounted at `/qdrant/storage`). |
| `MEDRAG_CORPUS_DIR` | Host path to the corpus root holding `catalog.db` and `data/` (mounted at `/app/corpus`). |
| `MEDRAG_QDRANT_STORES` | Comma-separated logical stores that actually exist, e.g. `main,standards,expand`. |
| `MEDRAG_CATALOG_DB` | Catalog SQLite path (in-container default `/app/corpus/catalog.db`). |
| `MEDRAG_DATA_DIR` | Chunks / OCR text (in-container default `/app/corpus/data`). |

Logical store → Qdrant collection is `{collection}_{store}` (base `collection` from `config.yaml`, default `medical_library`).

## 4. Performance and health

| Variable | Default | Purpose |
|---|---|---|
| `HEALTH_CACHE_TTL_SEC` | `5` | TTL for `/api/health` dependency probing. **Important:** without this, every health request fanned out to Ollama + MedicalRAG; measured p50 was 17 s at 50 concurrent users with 29 % timeouts. With caching + single-flight it is ~1 ms. |
| `MEDRAG_HEALTH_TIMEOUT_SEC` | `3` | Per-probe timeout for the MedicalRAG health check. |
| `BACKEND_START_PERIOD` | `180s` | Whisper model load budget before healthcheck failures count. |
| `MEDRAG_START_PERIOD` | `120s` | MedicalRAG warm-up budget. |
| `QDRANT_START_PERIOD` | `900s` | Qdrant does not bind HTTP until all collections are recovered; a multi-GB corpus needs minutes. |

Two probe levels exist by design:

- `GET /api/live` (backend) and `GET /live` (medrag) — liveness only, zero dependencies. **Load balancers and container healthchecks use these.**
- `GET /api/health` and `GET /health` — readiness, including dependency status. Cached.

## 5. Scaling and load balancing

| Variable | Default | Purpose |
|---|---|---|
| `BACKEND_REPLICAS` / `MEDRAG_REPLICAS` / `FRONTEND_REPLICAS` | `1` | Replica counts; the gateway discovers them via Docker DNS. |
| `GATEWAY_PUBLISH_PORT` | `8080` | Host port for the single public entrypoint. |
| `LB_AI_RATE` / `LB_AI_BURST` | `30r/s` / `200` | Per-IP rate limit on `/api/{transcribe,dictate,report,chat,vision}`. |
| `LB_MAX_CONN_PER_IP` | `100` | Per-IP concurrent connection cap. |
| `LB_READ_TIMEOUT` / `LB_SEND_TIMEOUT` | `600s` | ASR/LLM calls legitimately run for minutes. |
| `LB_MAX_BODY_SIZE` | `256m` | Dictation audio uploads. |
| `LB_NEXT_UPSTREAM_TRIES` | `3` | Retry another replica on failure. |
| `LB_KEEPALIVE` | `64` | Upstream keepalive connections. |

The gateway uses `least_conn`, not round-robin: ASR/LLM requests have wildly uneven service times, so a replica already busy transcribing must not be handed more work.

## 6. Precedence

1. Process environment (including `environment:` in compose — beats `env_file`)
2. Repo-root `.env`
3. Service-local `.env` (backend only)
4. `.env.example` (documented defaults; loaded last so a bare checkout runs)
5. `config.yaml` (MedicalRAG) / `models.yaml` (backend registry)
6. In-code default constant

Secrets (`ASR_AGENT_SECRET`, `HF_TOKEN`, SMTP/Twilio) belong in `.env`, which is gitignored and excluded from the Docker build context.

---

## 7. Security and durability knobs

Added by the hardening pass. Full rationale in `docs/core/SECURITY.md`; every
key is listed with its default in `.env.example`.

| Area | Keys | Notes |
| --- | --- | --- |
| Token signing | `ASR_AGENT_SECRET`, `ASR_AGENT_TOKEN_TTL` | **Set the secret before running >1 backend replica** — otherwise each worker signs with its own random key and tokens fail across replicas. |
| First-run admin | `ASR_AGENT_ADMIN_USER`, `ASR_AGENT_ADMIN_PASSWORD`, `ADMIN_CREDENTIALS_FILE`, `ASR_AGENT_ALLOW_INSECURE_ADMIN` | No well-known default password; a random one is generated and written to a `0600` file unless you set one. |
| Login throttle | `LOGIN_THROTTLE_BACKEND`, `LOGIN_MAX_ATTEMPTS`, `LOGIN_WINDOW_SEC`, `LOGIN_LOCKOUT_SEC`, `LOGIN_THROTTLE_MAX_KEYS`, `REDIS_URL`, `LOGIN_THROTTLE_REDIS_PREFIX` | Defaults to a store shared across replicas (SQLite, or Redis when `REDIS_URL` is set). `memory` is per-process and only correct at one replica. |
| SQLite concurrency | `DB_JOURNAL_MODE`, `DB_SYNCHRONOUS`, `DB_BUSY_TIMEOUT_MS` | WAL lets readers proceed during a write; `busy_timeout` makes a concurrent writer wait rather than fail with "database is locked". Both matter once replicas share `backend/data/app.db`. |
| Gateway TLS | `GATEWAY_TLS_*`, `GATEWAY_HSTS_MAX_AGE` | Applied by the `docker-compose.tls.yml` overlay (`./deploy.sh --tls`), which also publishes the HTTPS port. |
| Agent/memory runtime | `runtime.agent_*`, `runtime.memory_*` in `backend/models.yaml` | Loop iteration cap, per-call timeouts, generation budgets, memory window/eviction/purge interval. Not env vars — they live with the model registry they tune. |
| Prompts | `PROMPTS_DIR` | System prompts are editable data files under `backend/data/prompts/`, with in-code fallbacks. |
