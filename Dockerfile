# =============================================================================
# ASR-Agent — production Docker images (GPU server)
# Build:  docker compose build
# Run:    docker compose up -d
# =============================================================================

# ── Backend (FastAPI + Whisper large-v3 + CUDA) ──────────────────────────────
# CUDA 12.8+ is required for PyTorch 2.7+ wheels that carry sm_120 (Blackwell /
# RTX 50-series) kernels. Older 12.4 images silently fall back to no GPU
# kernels on those cards. Match TORCH_INDEX in Makefile/.env.example if you
# change this.
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04 AS backend

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models \
    PORT=8010 \
    PYTHONPATH=/app/backend

# Debian/Ubuntu mirrors intermittently return 502/EOF on constrained links,
# which fails the whole build after the multi-GB CUDA layers already pulled.
# Retry acquisition and the apt invocation itself so a transient mirror error
# does not discard that work.
RUN printf 'Acquire::Retries "10";\nAcquire::http::Timeout "60";\nAcquire::https::Timeout "60";\n' \
        > /etc/apt/apt.conf.d/99retries \
    # The CUDA base image ships an NVIDIA apt repo, and `apt-get update` fails
    # the whole build if it is unreachable -- which it is from networks NVIDIA
    # geo-blocks (observed: "403 Forbidden ... repository is not signed" from
    # an Iranian host). Nothing installed below comes from that repo; the CUDA
    # runtime is already baked into the image. Disabling it makes the build
    # depend only on Ubuntu mirrors.
    && rm -f /etc/apt/sources.list.d/cuda*.list \
             /etc/apt/sources.list.d/nvidia*.list 2>/dev/null || true \
    && for i in 1 2 3 4 5; do \
         apt-get update && break || { echo "apt-get update retry $i"; sleep 15; }; \
       done \
    && for i in 1 2 3 4 5; do \
         apt-get install -y --no-install-recommends \
             python3.11 python3.11-venv python3-pip \
             ffmpeg libsndfile1 curl ca-certificates \
         && break || { echo "apt-get install retry $i"; sleep 15; }; \
       done \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python3 \
    && python --version

WORKDIR /app

ARG TORCH_INDEX=https://download.pytorch.org/whl/cu128

COPY backend/requirements.txt /app/backend/requirements.txt
RUN python -m pip install --upgrade pip wheel \
    && pip install torch torchaudio --index-url "$TORCH_INDEX" \
    && pip install -r /app/backend/requirements.txt \
    && python -c "import torch; import transformers; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"

# Security dependencies live in their own layer, deliberately AFTER the heavy
# one above. Appending to backend/requirements.txt invalidates that layer and
# forces a full torch reinstall (~2GB) on every rebuild — which is both slow
# and a real failure mode when the network hiccups mid-download. Adding one
# here costs seconds instead.
#   cryptography -> backend/phi_crypto.py, AES-256-GCM for PHI at rest.
#     Stdlib has no AEAD and hand-rolling a cipher is not acceptable, so this
#     is the one place the project takes a crypto dependency.
#   psycopg      -> backend/dialect.py, Postgres beyond SQLite's single writer.
#   pydicom      -> backend/dicom_ingest.py, DICOM study metadata + rendering.
RUN pip install --no-cache-dir \
        "cryptography>=43.0.0" \
        "psycopg[binary]>=3.2" \
        "pydicom>=2.4" \
    && python -c "\
from cryptography.hazmat.primitives.ciphers.aead import AESGCM; \
import psycopg, pydicom; \
print('security/interop deps OK')"

COPY backend /app/backend
COPY scripts /app/scripts

WORKDIR /app/backend
EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -sf http://127.0.0.1:8010/api/health || exit 1

# Bind host/port/workers from env so the same image serves any topology.
#
# Workers default to 1: each worker process loads its *own* copy of the ASR /
# vision models, so N workers multiply VRAM (whisper-large-v3 alone is ~5.8 GB
# of an 8 GB card). Scale out with container replicas behind the gateway
# instead — see docs/core/DEPLOYMENT.md — or raise BACKEND_WORKERS on a host
# with VRAM to spare.
# BACKEND_TLS_ENABLED=1 makes the backend serve HTTPS directly, for
# deployments where the gateway->backend hop must also be encrypted (see
# docs/core/SECURITY.md). Off by default: the hop is a private compose
# network behind the terminator, and adding TLS there costs a handshake per
# connection for no gain when that network is trusted.
CMD ["sh", "-c", "\
if [ \"${BACKEND_TLS_ENABLED:-0}\" = \"1\" ]; then \
  echo 'backend: serving HTTPS (BACKEND_TLS_ENABLED=1)'; \
  exec python -m uvicorn app:app --host ${HOST:-0.0.0.0} --port ${PORT:-8010} \
       --workers ${BACKEND_WORKERS:-1} --backlog ${BACKEND_BACKLOG:-2048} \
       --ssl-keyfile ${BACKEND_TLS_KEY:-/etc/aranmed/certs/tls.key} \
       --ssl-certfile ${BACKEND_TLS_CERT:-/etc/aranmed/certs/tls.crt}; \
else \
  exec python -m uvicorn app:app --host ${HOST:-0.0.0.0} --port ${PORT:-8010} \
       --workers ${BACKEND_WORKERS:-1} --backlog ${BACKEND_BACKLOG:-2048}; \
fi"]


# ── MedicalRAG (Knowledge Engine microservice) ───────────────────────────────
# Installs every Python dependency automatically (medrag package + extras).
# CPU wheels by default so the image builds anywhere; switch to CUDA for GPU
# embeddings at build time:
#   docker compose build --build-arg MEDRAG_TORCH_INDEX=https://download.pytorch.org/whl/cu128 medrag
FROM python:3.11-slim AS medrag

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    HF_HOME=/models \
    MEDRAG_ROOT=/app \
    MEDRAG_PORT=8080

# Only runtime libs: curl for the healthcheck, libgomp1 for torch/sklearn
# OpenMP. No toolchain — every dependency ships prebuilt wheels, and pulling
# build-essential would add ~400 MB for nothing.
RUN printf 'Acquire::Retries "10";\nAcquire::http::Timeout "60";\nAcquire::https::Timeout "60";\n' \
        > /etc/apt/apt.conf.d/99retries \
    # The CUDA base image ships an NVIDIA apt repo, and `apt-get update` fails
    # the whole build if it is unreachable -- which it is from networks NVIDIA
    # geo-blocks (observed: "403 Forbidden ... repository is not signed" from
    # an Iranian host). Nothing installed below comes from that repo; the CUDA
    # runtime is already baked into the image. Disabling it makes the build
    # depend only on Ubuntu mirrors.
    && rm -f /etc/apt/sources.list.d/cuda*.list \
             /etc/apt/sources.list.d/nvidia*.list 2>/dev/null || true \
    && for i in 1 2 3 4 5; do \
         apt-get update && break || { echo "apt-get update retry $i"; sleep 15; }; \
       done \
    && for i in 1 2 3 4 5; do \
         apt-get install -y --no-install-recommends curl ca-certificates libgomp1 \
         && break || { echo "apt-get install retry $i"; sleep 15; }; \
       done \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ARG MEDRAG_TORCH_INDEX=https://download.pytorch.org/whl/cpu

# Dependency layer first so source edits don't re-trigger the long install.
COPY requirements-medrag.txt /app/requirements-medrag.txt
RUN python -m pip install --upgrade pip wheel \
    && pip install torch --index-url "$MEDRAG_TORCH_INDEX" \
    && pip install -r /app/requirements-medrag.txt

COPY pyproject.toml /app/
COPY src /app/src
COPY config.yaml /app/config.yaml
# Ops tooling (model warmup, endpoint resolver) so a running container can
# pre-cache weights: docker compose exec medrag python /app/scripts/warmup_models.py
COPY scripts /app/scripts
# pyproject declares `readme`, but *.md is excluded from the build context
# (.dockerignore) to keep it small — provide a stub so the metadata resolves.
RUN printf 'MedicalRAG service image.\n' > /app/README.md \
    && pip install --no-deps -e . \
    && python -c "import medrag, fastapi, qdrant_client; print('medrag deps OK')"

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=5 \
    CMD curl -sf "http://127.0.0.1:${MEDRAG_PORT:-8080}/health" || exit 1

CMD ["sh", "-c", "python -m uvicorn medrag.interfaces.api:app --host ${MEDRAG_HOST:-0.0.0.0} --port ${MEDRAG_PORT:-8080}"]


# ── Frontend (Next.js production) ────────────────────────────────────────────
FROM node:20-alpine AS frontend-build

WORKDIR /app

# The npm registry is a build-time knob because registry.npmjs.org is not
# reachable from every network this deploys onto -- observed: repeated
# `npm error network ETIMEDOUT` from a host whose international routing is
# filtered, which fails the whole image build ~20 minutes in. Point this at
# any npm-compatible mirror; the default keeps upstream behaviour.
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG NPM_FETCH_RETRIES=5
ARG NPM_FETCH_TIMEOUT=300000

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm config set registry "$NPM_REGISTRY" \
    && npm config set fetch-retries "$NPM_FETCH_RETRIES" \
    && npm config set fetch-timeout "$NPM_FETCH_TIMEOUT" \
    && echo "npm registry: $(npm config get registry)" \
    && (npm ci 2>/dev/null || npm install)
COPY frontend .
RUN npm run build

FROM node:20-alpine AS frontend

WORKDIR /app
COPY --from=frontend-build /app ./

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=3000 \
    BACKEND_URL=http://backend:8010

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1

# npm start uses Unix env syntax; run node directly for Alpine compatibility.
CMD ["node", "server.js"]
