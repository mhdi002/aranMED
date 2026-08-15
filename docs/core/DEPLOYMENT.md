# AranMed Deployment & Operations

Companion docs: [`CORE_ARCHITECTURE_v1.md`](CORE_ARCHITECTURE_v1.md) · [`CONFIGURATION.md`](CONFIGURATION.md) · [`../MICROSERVICES.md`](../MICROSERVICES.md)

## 1. Topology

```
                          ┌───────────────────────────┐
   browser / API client ─▶│  gateway (nginx)          │  :${GATEWAY_PUBLISH_PORT}
                          │  least_conn, rate limits, │
                          │  600s AI timeouts         │
                          └────────┬─────────┬────────┘
                       /api/*      │         │   everything else
                                   ▼         ▼
                    ┌──────────────────┐  ┌──────────────────┐
                    │ backend  × N     │  │ frontend × N     │
                    │ ASR · reports ·  │  │ Next.js UI       │
                    │ rules · EHR      │  └──────────────────┘
                    └───┬────────┬─────┘
                        │        │
              ┌─────────▼──┐  ┌──▼───────────────┐
              │ medrag × N │  │ Ollama / vLLM    │
              │ Knowledge  │  │ (host or profile)│
              └─────┬──────┘  └──────────────────┘
                    ▼
              ┌───────────┐
              │  qdrant   │  real corpus, bind-mounted
              └───────────┘
```

Every arrow is an environment variable — see [`CONFIGURATION.md`](CONFIGURATION.md) §2.

## 2. One-command bring-up

```bash
cp .env.example .env
# point at your knowledge corpus (see §3), then:
docker compose up -d --build
```

That builds four images and starts the stack. Each image installs its own dependencies at build time — no manual `pip`, `npm`, or model setup:

| Image | Base | Installs | Size |
|---|---|---|---|
| `aranmed-backend` | `nvidia/cuda:12.8.1-cudnn-runtime` | CUDA torch + torchaudio, `backend/requirements.txt` (faster-whisper, transformers, FastAPI…) | ~10 GB |
| `aranmed-medrag` | `python:3.11-slim` | torch (CPU by default), `requirements-medrag.txt` (FlagEmbedding, qdrant-client, sentence-transformers…), then `pip install -e .` | ~2.7 GB |
| `aranmed-frontend` | `node:20-alpine` | `npm ci` + `next build` (multi-stage) | ~748 MB |
| `qdrant/qdrant` | upstream | — | — |

Open `http://localhost:${GATEWAY_PUBLISH_PORT}`.

### Warm the model cache before serving traffic

Both services fetch model weights **on first request**, into container volumes that are separate from any host cache. A fresh `docker compose up` therefore re-downloads multi-GB models *mid-request*:

| Service | Model | Size | Triggered by |
|---|---|---|---|
| medrag | `BAAI/bge-m3` | ~2.3 GB | first `/ask` |
| medrag | `BAAI/bge-reranker-v2-m3` | ~2.2 GB | first `/ask` (after retrieval) |
| backend | `openai/whisper-large-v3` | ~3 GB | first `/api/transcribe` |

Left implicit, the first real request blocks on the download — and because an aborted HTTP request cancels the transfer, every timeout-and-retry restarts rather than resumes. Warm the cache as a deployment step:

```bash
docker compose exec medrag python /app/scripts/warmup_models.py
```

### Or: use weights you already have on disk

If a model is already downloaded outside Docker (e.g. `./models/whisper-large-v3/` from a native run), skip the re-download entirely. `backend/providers/hf_asr.py::_resolve_model_source` already checks `<project_root>/models/<repo-name>` before hitting the network — the only missing piece was making that directory visible inside the container. It now is:

```dotenv
# .env — default is ./models, override only if your snapshots live elsewhere
LOCAL_MODELS_DIR=./models
```

Mounted read-only at `/app/models`. Verified: with a pre-downloaded `whisper-large-v3` snapshot, `POST /api/transcribe` through the gateway completed in **66 s with zero network traffic**, versus 600+ s and a stalled download without the mount.

The script resolves model ids from the same sources the services use — `MEDRAG_EMBED_MODEL` / `MEDRAG_RERANK_MODEL` (falling back to `medrag.config`, since the reranker is normally declared in `config.yaml` rather than as an env var) **and** the `model_id` of every enabled entry in the backend registry (`ASR_AGENT_REGISTRY_YAML`, i.e. `models.yaml`). It retries with resume on a flaky link and writes into the shared `hf-cache` volume so every replica benefits. Skipping it is safe, but the first knowledge query and the first transcription will each stall on a multi-GB fetch.

By default it fetches **only the artefacts the PyTorch runtime loads**. This matters more than it sounds: a plain `snapshot_download("BAAI/bge-m3")` also pulls ONNX and OpenVINO exports — roughly 2 GB that FlagEmbedding never opens. On a constrained link those crowd out the weights that are actually needed, and we observed exactly that failure mode: 3.5 GB cached, yet `pytorch_model.bin` still pointing at a 0-byte blob and `model.safetensors` only ~55 % complete. Pass `--all-formats` if you genuinely need the alternate runtimes.

### Useful variations

```bash
docker compose up -d --scale backend=4 --scale medrag=3   # horizontal scale
docker compose --profile ollama up -d                     # run Ollama in-compose
docker compose --profile vllm --profile vllm-embed up -d  # GPU LLM + embeddings
docker compose build --build-arg MEDRAG_TORCH_INDEX=https://download.pytorch.org/whl/cu128 medrag
```

## 3. Attaching a real knowledge corpus

The knowledge base is never bundled or mocked. Point the stack at an existing MedicalRAG install:

```dotenv
MEDRAG_QDRANT_STORAGE=D:/medical books/MedicalRAG/qdrant_storage
MEDRAG_CORPUS_DIR=D:/medical books/MedicalRAG
MEDRAG_QDRANT_STORES=main,standards,expand
```

`MEDRAG_QDRANT_STORES` must list only the logical stores that exist; the code no longer assumes a fixed five. Confirm what is actually present:

```bash
curl -s http://localhost:6333/collections
curl -s -X POST http://localhost:6333/collections/medical_library_expand/points/count \
     -H 'Content-Type: application/json' -d '{"exact":false}'
```

### Verified on a real corpus

| Collection | Points |
|---|---|
| `medical_library_expand` | 438,371 |
| `medical_library` | 117,011 |
| `medical_library_standards` | 5 |
| `medical_library_main` | 0 |

`GET /health` on the containerised medrag reported `chunks: 438376`, `embeddings.ok: true` (local bge-m3), `llm.ok: true` (Ollama via `host.docker.internal`).

### Memory requirements (important)

With the default **local CPU** path, MedicalRAG loads two models — `bge-m3` (~2.27 GB) and `bge-reranker-v2-m3` (~2.2 GB) — and reranks up to `retrieval.top_k_search` (default 50) passages. Together with the CUDA backend container, that exceeded an 8 GB Docker VM in testing: the kernel OOM-killer terminated the medrag worker mid-request, which surfaced to the client as a **dropped connection rather than an error** (`RestartCount` incremented, log line `Killed`, `State.OOMKilled` still `false` because the kill came from the VM's kernel, not a container cgroup limit).

Mitigations, in order of preference:

1. **Give Docker more RAM.** For the full local-CPU stack, budget **≥ 12 GB** for the Docker VM (Docker Desktop → Settings → Resources).
2. **Offload embeddings/reranking to GPU**: `docker compose --profile vllm-embed up -d`, then set `MEDRAG_EMBED_PROVIDER=vllm`. This removes both models from medrag's address space.
3. **Cap and attribute**: `MEDRAG_MEM_LIMIT` (default `6g`) is now set on the service so the failure is attributable to medrag instead of the OOM-killer choosing an arbitrary victim.
4. **Retrieve less**: lower `retrieval.top_k_search` in `config.yaml`.

### Storage performance caveat

Qdrant does **not** bind its HTTP port until every collection is recovered. Recovering a multi-GB index across a Windows bind mount (Docker Desktop's virtualised filesystem — it logs `Unrecognized filesystem`) is markedly slower than native I/O. Hence `QDRANT_START_PERIOD` defaults to `900s`. For sustained production throughput, either copy the storage into a Docker named volume or run Qdrant natively and point `DOCKER_QDRANT_URL` at it.

## 4. Health model

Two deliberately separate probe levels:

| Probe | Endpoint | Touches dependencies? | Used by |
|---|---|---|---|
| Liveness | `/api/live` (backend), `/live` (medrag) | No | load balancer, container healthcheck |
| Readiness | `/api/health`, `/health` | Yes (cached) | UI status pills, operators |

**Why this split matters.** `/api/health` fans out to Ollama and MedicalRAG. Probing per request collapsed under concurrency — measured p50 **17.4 s** at 50 users with **28.9 %** read timeouts. The fix is a TTL cache plus single-flight de-duplication (`HEALTH_CACHE_TTL_SEC`, default 5 s): N concurrent callers trigger at most one upstream probe. Measured after the fix: `/api/live` **0.5 ms**, `/api/health` **0.9 ms**, `/api/templates` **5.2 ms**.

### `/api/models` — three compounding costs, three fixes

Registry introspection took a flat **3.0 s** per call. Three separate causes, each fixed:

1. **Serial probing.** `Registry.health()` awaited each provider in a loop, so latency was the *sum* of every provider's timeout. Now `asyncio.gather` with a per-provider `asyncio.wait_for` (`PROVIDER_HEALTH_TIMEOUT_SEC`, default 2 s) — latency is the *slowest* probe, not the total.
2. **A health probe reusing an inference timeout.** `triton_asr.health()` used `min(self.timeout, 10.0)`, where `self.timeout` is the ASR inference budget (often 120 s). An absent optional Triton therefore cost ~2.5 s on every call. It now uses a dedicated short probe timeout (`TRITON_HEALTH_CONNECT_TIMEOUT_SEC`, default 0.5 s), mirroring the Ollama provider.
3. **A TTL shorter than the worker round-robin.** Caches are per-worker. With 12 uvicorn workers, a given worker only sees every 12th request, so a 5 s TTL always expired before its next turn and the cache never hit. `/api/models` therefore has its own longer window (`MODELS_CACHE_TTL_SEC`, default 30 s) — registry composition only changes on model load/unload.

Measured after all three: **~1.4 ms** on cache hit (11 of 12 steady-state requests), ~1.5 s on a cold worker, ~3.4 s on a worker's very first call (one-time CUDA/torch initialisation).

This is a general lesson for multi-worker deployments: **a per-process cache is only useful if its TTL exceeds the time for the load balancer to cycle back to that process.**

### Optional dependencies must not inherit inference timeouts

The same anti-pattern bit twice more, and both were genuine availability bugs:

| Call site | Was | Now |
|---|---|---|
| `report_rules.consult_medrag_naming_rules()` | shared client's `MEDRAG_TIMEOUT_SEC` (**1200 s**) | `REPORT_RULES_MEDRAG_TIMEOUT_SEC`, default **20 s** |
| `clinical_safety.triage_with_medrag()` | same shared client timeout | `CLINICAL_SAFETY_MEDRAG_TIMEOUT_SEC`, default **30 s** |

Both are *optional enhancements* — the local naming-rules excerpt and the local regex triage have already run and are sufficient on their own. But because both reused the client timeout sized for full RAG inference, a MedicalRAG that was merely **warming up** (downloading its reranker) silently stalled `POST /api/report`. Observed directly: the gateway logged `499` after the client gave up at 300 s while the backend was still blocked inside that consult.

The rule: **a timeout should express the caller's patience, not the callee's worst case.** A best-effort enrichment gets seconds; the primary inference path gets minutes. Both now fail quiet and the report proceeds on local rules.

## 5. Load balancing

`deploy/nginx/nginx.conf.template` is rendered by nginx's `envsubst` entrypoint, so all tuning is environment-driven (`NGINX_ENVSUBST_FILTER` restricts substitution to our own `GATEWAY_/BACKEND_/FRONTEND_/LB_` prefixes so nginx's own `$variables` survive).

Design choices:

- **`least_conn`, not round-robin.** ASR/LLM service times vary by orders of magnitude; a replica mid-transcription must not receive more work.
- **Separate rate limits by cost class.** `/api/{transcribe,dictate,report,chat,vision}` are rate- and connection-limited per IP; health/templates/static are not.
- **Long timeouts on AI paths** (`600s`) with `proxy_request_buffering off` so large audio uploads stream through.
- **`proxy_next_upstream`** retries another replica on connect/5xx failures.
- **Replica discovery via Docker DNS**, so `--scale backend=N` needs no config change.

## 6. Measured capacity

Command:

```bash
python scripts/loadtest.py --group api --users 1000 --processes 6 --duration 30
```

| Run | Users | Requests | Throughput | Error rate | p50 | p95 |
|---|---|---|---|---|---|---|
| Before health fix, 4 workers | 50 | 98 | 3.0 rps | **40.8 %** | 6.1 s | 26.6 s |
| After health fix, 4 workers | 200 | 3,499 | 150 rps | 0.0 %¹ | 0.35 s | 4.1 s |
| After health fix, 12 workers | 1,000 | 12,400 | 382 rps | **0.0 %** | 1.25 s | 5.5 s |
| Multi-process client, 12 workers | 1,000 | 17,936 | **529 rps** | **0.0 %** | 0.30 s | 5.3 s |

¹ API scenarios only; UI scenarios in that run were 404s because the test targeted the backend directly — since fixed by the `--group` flag.

**Honest reading of these numbers.** Single-request latency is 0.5–5 ms, and the stack sustained 1,000 concurrent users with a 0 % error rate — it degrades gracefully rather than failing. The multi-second p95 is *not* server saturation: the load generator ran on the same 16-core machine as the server, so client and server contended for CPU. A trustworthy capacity figure requires the generator on a separate host. Use `--processes` (one asyncio process cannot saturate a fast server at high concurrency).

## 7. Verification scripts

All are environment-driven via `scripts/_endpoints.py`.

```bash
python scripts/_endpoints.py                 # show every resolved endpoint
python scripts/verify_asr.py                 # real audio → transcript, 8 assertions
python scripts/verify_knowledge.py           # corpus is real, loaded and queryable, 10 assertions
python scripts/warmup_models.py              # pre-cache model weights
python scripts/loadtest.py --users 1000 --processes 6
python scripts/full_system_verify.py         # 14-point system matrix
pytest tests/ && PYTHONPATH=src pytest tests/medrag/
```

`verify_asr.py` auto-discovers the audio file in the repo root (override with `--audio` / `ASR_VERIFY_AUDIO`), asserts backend reachability, ASR model configuration, transcript length, real word content, time budget, and English normalisation, then writes `reports/asr_verify.json`.

## 8. Operations

```bash
docker compose ps                      # status + health
docker compose logs -f backend         # follow a service
docker compose up -d --scale backend=4 # scale without downtime
docker compose down                    # stop (volumes and corpus preserved)
```

Troubleshooting:

| Symptom | Cause | Action |
|---|---|---|
| qdrant `unhealthy` for minutes | Recovering a large index; HTTP not yet bound | Expected — raise `QDRANT_START_PERIOD`; watch `docker compose logs qdrant` |
| medrag `/health` shows `embeddings.ok: false` | bge-m3 not yet downloaded, or vLLM embed server unreachable | First call downloads the model into the `hf-cache` volume; or start `--profile vllm-embed` |
| backend healthy but `/api/health` shows `medrag.ok: false` | MedicalRAG not running or `MEDRAG_API_URL` wrong | `docker compose ps medrag`; check `DOCKER_MEDRAG_API_URL` |
| 503 on `/api/chat` | Text-only chat requires MedicalRAG | Start medrag; unrelated to ASR/report paths |
| GPU not used in container | Missing NVIDIA Container Toolkit | Install it; or set `GPU_COUNT` / drop the GPU reservation |
