#!/usr/bin/env bash
# =============================================================================
# AranMed — one-command full-stack deployment (Linux / macOS / WSL).
#
# Builds every image (backend, frontend, medrag, triton, gateway — each
# installs its own deps at build time, no manual pip/npm), downloads the
# required models (Whisper into the shared hf-cache volume, Ollama LLM if
# requested), and brings the whole stack up under Docker Compose.
#
# Usage:
#   ./deploy.sh                       # core stack: gateway+backend+triton+medrag+qdrant+frontend
#   ./deploy.sh --with-ollama         # + Ollama in-compose (else point OLLAMA_HOST at a host)
#   ./deploy.sh --with-vllm           # + GPU generation server (OpenAI-compatible)
#   ./deploy.sh --with-vllm-embed     # + GPU embedding server
#   ./deploy.sh --with-ollama --with-vllm --with-vllm-embed   # everything
#   ./deploy.sh --tls                 # terminate HTTPS at the gateway
#                                     #   (needs deploy/nginx/certs/tls.{crt,key})
#   ./deploy.sh --skip-models         # skip the Whisper/Ollama prefetch step
#   ./deploy.sh --no-up               # build + prefetch only, don't start containers
#   ./deploy.sh --install-docker      # offer to install Docker if missing (asks first)
#   ./deploy.sh --yes                 # don't pause for confirmations
#
# Re-run any time — every step is idempotent (build cache, existing .env,
# already-pulled models, and `docker compose up -d` are all safe to repeat).
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

WITH_OLLAMA=0
WITH_VLLM=0
WITH_VLLM_EMBED=0
SKIP_MODELS=0
NO_UP=0
INSTALL_DOCKER=0
ASSUME_YES=0
WITH_TLS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-ollama)     WITH_OLLAMA=1 ;;
    --with-vllm)       WITH_VLLM=1 ;;
    --with-vllm-embed) WITH_VLLM_EMBED=1 ;;
    --tls)             WITH_TLS=1 ;;
    --skip-models)     SKIP_MODELS=1 ;;
    --no-up)           NO_UP=1 ;;
    --install-docker)  INSTALL_DOCKER=1 ;;
    --yes|-y)          ASSUME_YES=1 ;;
    -h|--help)         sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

c_cyan="\033[36m"; c_yel="\033[33m"; c_grn="\033[32m"; c_red="\033[31m"; c_dim="\033[90m"; c_off="\033[0m"
say()  { printf "${c_cyan}══ %s ══${c_off}\n" "$*"; }
step() { printf "${c_yel}[%s] %s${c_off}\n" "$1" "$2"; }
ok()   { printf "${c_grn}    ✓ %s${c_off}\n" "$*"; }
warn() { printf "${c_red}    ⚠ %s${c_off}\n" "$*"; }
die()  { printf "${c_red}ERROR: %s${c_off}\n" "$*" >&2; exit 1; }

confirm() {
  [[ $ASSUME_YES -eq 1 ]] && return 0
  read -r -p "$1 [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

say "AranMed — full-stack deployment"
echo "  Project root : $ROOT"

# ── 1) Docker + Compose v2 ───────────────────────────────────────────────────
step "1/6" "Checking Docker"
if ! command -v docker >/dev/null 2>&1; then
  warn "Docker not found."
  if [[ $INSTALL_DOCKER -eq 1 ]] && confirm "Install Docker now via get.docker.com (requires sudo)?"; then
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER" || true
    warn "Log out/in (or run 'newgrp docker') for group membership to take effect, then re-run this script."
    exit 0
  else
    die "Install Docker (https://docs.docker.com/engine/install/) then re-run, or pass --install-docker."
  fi
fi
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 plugin not found — update Docker."
ok "Docker $(docker --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1), Compose $(docker compose version --short 2>/dev/null || echo '?')"

if ! docker info >/dev/null 2>&1; then
  die "Docker daemon isn't reachable (is Docker Desktop / dockerd running?)."
fi

if command -v nvidia-smi >/dev/null 2>&1; then
  ok "GPU detected: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
else
  warn "nvidia-smi not found on host — backend/triton/vllm need GPU passthrough (nvidia-container-toolkit on Linux, or Docker Desktop WSL2 GPU support). They will fail to start without it."
fi

# ── 2) .env ───────────────────────────────────────────────────────────────
step "2/6" "Environment file"
if [[ ! -f "$ROOT/.env" ]]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  ok "Wrote .env from .env.example — review MEDRAG_QDRANT_STORAGE/MEDRAG_CORPUS_DIR (your knowledge corpus), HF_TOKEN, and OLLAMA_MODEL before continuing."
else
  printf "${c_dim}    .env already present — leaving as-is.${c_off}\n"
fi
# A key defined twice in .env silently resolves to one of the two values,
# and the loser is usually the one the operator meant. This bites hardest on
# path keys -- a duplicate MEDRAG_QDRANT_STORAGE can mount an empty local
# directory instead of the real knowledge corpus, and the stack comes up
# "healthy" serving zero chunks. Warn loudly rather than guessing.
_dupes="$(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$ROOT/.env" 2>/dev/null | sort | uniq -d | tr -d '=')"
if [[ -n "$_dupes" ]]; then
  warn "Duplicate keys in .env — the later definition wins, which may not be what you intended:"
  while read -r k; do
    [[ -z "$k" ]] && continue
    printf "        %s\n" "$k"
    grep -nE "^${k}=" "$ROOT/.env" | sed 's/^/          line /'
  done <<< "$_dupes"
  warn "Comment out the stale definition(s) and re-run, or continue if this is intentional."
fi

set -a; source "$ROOT/.env" 2>/dev/null || true; set +a

# Corpus sanity: a bind-mounted Qdrant path that doesn't exist (or is empty)
# means the stack will start and report healthy while serving no knowledge.
_qdrant_path="${MEDRAG_QDRANT_STORAGE:-./qdrant_storage}"
if [[ ! -d "$_qdrant_path" ]]; then
  warn "MEDRAG_QDRANT_STORAGE points at '$_qdrant_path', which does not exist — MedicalRAG will serve 0 chunks."
elif [[ -z "$(ls -A "$_qdrant_path/collections" 2>/dev/null)" ]]; then
  warn "MEDRAG_QDRANT_STORAGE ('$_qdrant_path') has no collections — MedicalRAG will serve 0 chunks."
fi

PROFILE_ARGS=()
[[ $WITH_OLLAMA -eq 1 ]]     && PROFILE_ARGS+=(--profile ollama)
[[ $WITH_VLLM -eq 1 ]]       && PROFILE_ARGS+=(--profile vllm)
[[ $WITH_VLLM_EMBED -eq 1 ]] && PROFILE_ARGS+=(--profile vllm-embed)

# TLS is an opt-in compose overlay (see docker-compose.tls.yml). Every compose
# call goes through dc() so the overlay can't be applied to some commands and
# silently missed by others.
FILE_ARGS=()
if [[ $WITH_TLS -eq 1 ]]; then
  FILE_ARGS+=(-f docker-compose.yml -f docker-compose.tls.yml)
  cert="${GATEWAY_TLS_CERT_DIR:-./deploy/nginx/certs}"
  if [[ ! -r "$cert/tls.crt" || ! -r "$cert/tls.key" ]]; then
    warn "--tls given but $cert/tls.{crt,key} not found."
    echo "  Generate a local test certificate with:"
    echo "    mkdir -p $cert && openssl req -x509 -newkey rsa:2048 -nodes \\"
    echo "      -keyout $cert/tls.key -out $cert/tls.crt -days 365 -subj '/CN=localhost'"
    die "no certificate to serve"
  fi
  ok "TLS enabled — HTTPS on ${GATEWAY_TLS_PUBLISH_PORT:-8443}"
fi
dc() { docker compose "${FILE_ARGS[@]}" "$@"; }

# ── 3) Build images (backend, frontend, medrag, triton — deps installed in-image) ──
step "3/6" "Building images (backend + frontend + medrag + triton; each installs its own deps)"
dc "${PROFILE_ARGS[@]}" build
ok "Images built"

# ── 4) Model downloads ──────────────────────────────────────────────────────
if [[ $SKIP_MODELS -eq 0 ]]; then
  step "4/6" "Downloading models"
  echo "  Whisper large-v3 → shared hf-cache volume (used by backend + triton)"
  # MSYS_NO_PATHCONV: on git-bash/Windows, leading-slash args get silently
  # rewritten into host Windows paths before reaching docker (e.g.
  # /app/scripts/x.py -> C:/Program Files/Git/app/scripts/x.py). No-op elsewhere.
  MSYS_NO_PATHCONV=1 dc run --rm backend python /app/scripts/install_models.py --skip-ollama \
    || warn "Whisper prefetch failed — it will lazily download on first request instead."

  OLLAMA_MODEL_NAME="${OLLAMA_MODEL:-qwen3.5-9b:latest}"
  pull_or_guide_ollama() {
    local list_cmd=("$@")
    if "${list_cmd[@]}" 2>/dev/null | grep -qF "$OLLAMA_MODEL_NAME"; then
      ok "Ollama already has '$OLLAMA_MODEL_NAME' — skipping pull"
      return 0
    fi
    return 1
  }
  if [[ $WITH_OLLAMA -eq 1 ]]; then
    echo "  Ollama model '$OLLAMA_MODEL_NAME' → in-compose ollama container"
    dc --profile ollama up -d ollama
    for i in $(seq 1 30); do
      dc exec -T ollama ollama list >/dev/null 2>&1 && break
      sleep 2
    done
    if ! pull_or_guide_ollama dc exec -T ollama ollama list; then
      dc exec -T ollama ollama pull "$OLLAMA_MODEL_NAME" || {
        warn "Ollama pull of '$OLLAMA_MODEL_NAME' failed — it isn't on the public registry."
        [[ -f "$ROOT/Modelfile_qwen" || -f "$ROOT/Modelfile" ]] && \
          warn "This repo ships a local Modelfile — build it instead, e.g.: docker compose exec ollama ollama create ${OLLAMA_MODEL_NAME%%:*} -f /Modelfile_qwen (mount the Modelfile + GGUF into the container first)."
      }
    fi
  elif command -v ollama >/dev/null 2>&1; then
    echo "  Ollama model '$OLLAMA_MODEL_NAME' → host Ollama"
    if ! pull_or_guide_ollama ollama list; then
      ollama pull "$OLLAMA_MODEL_NAME" || {
        warn "Host 'ollama pull $OLLAMA_MODEL_NAME' failed — it isn't on the public registry."
        [[ -f "$ROOT/Modelfile_qwen" || -f "$ROOT/Modelfile" ]] && \
          warn "This repo ships a local Modelfile — build it instead: ollama create ${OLLAMA_MODEL_NAME%%:*} -f Modelfile_qwen (edit its FROM path to your GGUF first)."
      }
    fi
  else
    warn "No --with-ollama and no host 'ollama' CLI found. Install Ollama (https://ollama.com), then either 'ollama pull $OLLAMA_MODEL_NAME' or build the repo's local Modelfile."
  fi

  if [[ $WITH_VLLM -eq 1 || $WITH_VLLM_EMBED -eq 1 ]]; then
    echo "  vLLM/vllm-embed models download automatically from Hugging Face on first container start (cached in hf-cache volume)."
    [[ -z "${HF_TOKEN:-}" ]] && warn "HF_TOKEN is unset in .env — gated HF models (if any) will fail to download."
  fi
  ok "Model downloads complete"
else
  printf "${c_dim}[4/6] Skipping model downloads (--skip-models)${c_off}\n"
fi

# ── 5) Bring the stack up ───────────────────────────────────────────────────
if [[ $NO_UP -eq 1 ]]; then
  say "Build + model prefetch complete (--no-up). Start later with:"
  echo "  docker compose ${PROFILE_ARGS[*]} up -d"
  exit 0
fi

step "5/6" "Starting the stack"
dc "${PROFILE_ARGS[@]}" up -d
ok "Containers started"

# nginx resolves upstream container IPs once at its own startup. If gateway
# itself wasn't recreated but backend/medrag/etc. were (e.g. a re-run of this
# script after a code change), it's left pointing at dead IPs -> 502s despite
# every backend service reporting healthy. Force it to re-resolve.
dc restart gateway >/dev/null 2>&1 || true

# ── 6) Wait for health ──────────────────────────────────────────────────────
step "6/6" "Waiting for services to become healthy"
GATEWAY_PORT="${GATEWAY_PUBLISH_PORT:-8090}"
if [[ $WITH_TLS -eq 1 ]]; then
  # Plain HTTP now 301s to HTTPS, so probe the TLS listener directly.
  # -k because a local/self-signed cert is the normal case for a first run.
  HEALTH_URL="https://localhost:${GATEWAY_TLS_PUBLISH_PORT:-8443}/api/health"
  CURL_OPTS=(-sfk)
  APP_URL="https://localhost:${GATEWAY_TLS_PUBLISH_PORT:-8443}"
else
  HEALTH_URL="http://localhost:${GATEWAY_PORT}/api/health"
  CURL_OPTS=(-sf)
  APP_URL="http://localhost:${GATEWAY_PORT}"
fi
healthy=0
for i in $(seq 1 60); do
  if curl "${CURL_OPTS[@]}" "$HEALTH_URL" >/dev/null 2>&1; then
    healthy=1; break
  fi
  sleep 5
done

echo
dc ps
echo
if [[ $healthy -eq 1 ]]; then
  say "AranMed is up"
  echo "  App  → ${APP_URL}"
  echo "  API  → ${APP_URL}/api/health"
else
  warn "Gateway didn't answer /api/health within 5 minutes — check: docker compose logs -f"
fi
echo "  Logs → docker compose logs -f backend frontend triton medrag"
echo "  Stop → docker compose down"
