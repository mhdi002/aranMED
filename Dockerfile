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

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip \
        ffmpeg libsndfile1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python3

WORKDIR /app

ARG TORCH_INDEX=https://download.pytorch.org/whl/cu128

COPY backend/requirements.txt /app/backend/requirements.txt
RUN python -m pip install --upgrade pip wheel \
    && pip install torch torchaudio --index-url "$TORCH_INDEX" \
    && pip install -r /app/backend/requirements.txt \
    && python -c "import torch; import transformers; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"

COPY backend /app/backend
COPY scripts /app/scripts

WORKDIR /app/backend
EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -sf http://127.0.0.1:8010/api/health || exit 1

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8010"]


# ── Frontend (Next.js production) ────────────────────────────────────────────
FROM node:20-alpine AS frontend-build

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci 2>/dev/null || npm install
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
