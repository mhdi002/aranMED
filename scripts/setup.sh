#!/usr/bin/env bash
# One-shot bootstrap for the ASR-Agent project (Linux/macOS).
#
# Usage:
#   ./scripts/setup.sh                  # full setup, interactive model picker
#   ./scripts/setup.sh --auto           # non-interactive (pick first match)
#   ./scripts/setup.sh --skip-models    # skip the model picker
#   ./scripts/setup.sh --skip-frontend  # backend only
#   ./scripts/setup.sh --no-start       # install but don't launch
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ── resolve Python 3.10+ ──────────────────────────────────────────────────────
if [[ -n "${PYTHON:-}" ]]; then
  PY_BIN="$PYTHON"
elif command -v python3.10 >/dev/null 2>&1; then
  PY_BIN="python3.10"
elif command -v python3 >/dev/null 2>&1; then
  PY_BIN="python3"
else
  echo "ERROR: python3 not found. Install Python 3.10+." >&2; exit 1
fi
# If pyenv is active, prefer its python for CUDA / torch compatibility
if command -v pyenv >/dev/null 2>&1; then
  PYENV_PY="$(pyenv which python3 2>/dev/null || true)"
  [[ -n "$PYENV_PY" ]] && PY_BIN="$PYENV_PY"
fi

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
AUTO=0
SKIP_MODELS=0
SKIP_FRONTEND=0
NO_START=0
EXTRA_GGUF_DIRS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --auto)          AUTO=1 ;;
    --skip-models)   SKIP_MODELS=1 ;;
    --skip-frontend) SKIP_FRONTEND=1 ;;
    --no-start)      NO_START=1 ;;
    --gguf-dir)      EXTRA_GGUF_DIRS+=("$2"); shift ;;
    -h|--help)
      sed -n '1,12p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

c_cyan="\033[36m"; c_yel="\033[33m"; c_grn="\033[32m"; c_red="\033[31m"; c_dim="\033[90m"; c_off="\033[0m"
say()  { printf "${c_cyan}══ %s ══${c_off}\n" "$*"; }
step() { printf "${c_yel}[%s] %s${c_off}\n" "$1" "$2"; }
ok()   { printf "${c_grn}    ✓ %s${c_off}\n" "$*"; }
warn() { printf "${c_red}    ⚠ %s${c_off}\n" "$*"; }

say "ASR-Agent setup"
echo "  Project root : $ROOT"
echo "  Python       : $PY_BIN  ($(${PY_BIN} --version 2>&1))"

# ─────────────────────────────────────────────────────────────────────────────
# 1) venv
# ─────────────────────────────────────────────────────────────────────────────
VENV="$ROOT/.venv"
if [[ ! -d "$VENV" ]]; then
  step "1/6" "Creating virtualenv at $VENV"
  "$PY_BIN" -m venv "$VENV"
  ok "venv created"
else
  printf "${c_dim}[1/6] venv already present — reusing.${c_off}\n"
fi
PY="$VENV/bin/python"

# ─────────────────────────────────────────────────────────────────────────────
# 2) Backend Python dependencies
# ─────────────────────────────────────────────────────────────────────────────
step "2/6" "Installing backend Python requirements"
"$PY" -m pip install --upgrade pip wheel setuptools --quiet
"$PY" -m pip install -r "$ROOT/backend/requirements.txt" --quiet
ok "core backend requirements installed"

# omnilingual-asr (fairseq2 ecosystem) — separate because it pulls torch 2.8+
if ! "$PY" -c "import omnilingual_asr" 2>/dev/null; then
  echo "  Installing omnilingual-asr + fairseq2 (this may take a few minutes)..."
  "$PY" -m pip install omnilingual-asr --quiet
  ok "omnilingual-asr installed"
else
  printf "${c_dim}    omnilingual-asr already installed.${c_off}\n"
fi

# edge-tts + pydub used by tests for audio synthesis
if ! "$PY" -c "import edge_tts" 2>/dev/null; then
  "$PY" -m pip install edge-tts pydub --quiet
  ok "edge-tts + pydub installed"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 3) Pre-populate fairseq2 cache so omniASR weights aren't re-downloaded
#    Looks for the .pt file in common locations.
# ─────────────────────────────────────────────────────────────────────────────
step "3/6" "Pre-seeding fairseq2 asset cache from local model files"
_seed_fairseq2_cache() {
  local pt_file tok_file
  # Search common paths
  for candidate in \
      "$HOME/models/omniASR-LLM-300M/omniASR-LLM-300M.pt" \
      "$HOME/models/omniASR-LLM-1B/omniASR-LLM-1B.pt" \
      "$HOME/models/omniASR-LLM-7B/omniASR-LLM-7B.pt"; do
    [[ -f "$candidate" ]] || continue
    local model_dir
    model_dir="$(dirname "$candidate")"
    local pt_name
    pt_name="$(basename "$candidate")"

    # Find matching tokenizer
    tok_file=""
    for tf in "$model_dir/omniASR_tokenizer.model" "$model_dir/omniASR_tokenizer_v7.model"; do
      [[ -f "$tf" ]] && tok_file="$tf" && break
    done
    [[ -z "$tok_file" ]] && continue

    # Compute fairseq2 cache keys (SHA1 of original fbaipublicfiles URLs)
    local pt_url tok_url pt_hash tok_hash
    pt_url="https://dl.fbaipublicfiles.com/mms/${pt_name}"
    tok_url="https://dl.fbaipublicfiles.com/mms/$(basename "$tok_file")"
    pt_hash="$("$PY" -c "from hashlib import sha1; print(sha1('${pt_url}'.encode()).hexdigest()[:24])")"
    tok_hash="$("$PY" -c "from hashlib import sha1; print(sha1('${tok_url}'.encode()).hexdigest()[:24])")"

    local cache="$HOME/.cache/fairseq2/assets"
    if [[ ! -f "$cache/$pt_hash/$pt_name" ]]; then
      mkdir -p "$cache/$pt_hash"
      ln -f "$candidate" "$cache/$pt_hash/$pt_name" 2>/dev/null \
        || cp "$candidate" "$cache/$pt_hash/$pt_name"
      ok "Seeded $pt_name → fairseq2 cache"
    else
      printf "${c_dim}    $pt_name already in fairseq2 cache.${c_off}\n"
    fi
    if [[ ! -f "$cache/$tok_hash/$(basename "$tok_file")" ]]; then
      mkdir -p "$cache/$tok_hash"
      ln -f "$tok_file" "$cache/$tok_hash/$(basename "$tok_file")" 2>/dev/null \
        || cp "$tok_file" "$cache/$tok_hash/$(basename "$tok_file")"
      ok "Seeded $(basename "$tok_file") → fairseq2 cache"
    else
      printf "${c_dim}    $(basename "$tok_file") already in fairseq2 cache.${c_off}\n"
    fi
  done
}
_seed_fairseq2_cache

# ─────────────────────────────────────────────────────────────────────────────
# 4) Model discovery + selection
# ─────────────────────────────────────────────────────────────────────────────
if [[ $SKIP_MODELS -eq 0 ]]; then
  step "4/6" "Scanning system for available models (core / asr / vision)"
  args=()
  [[ $AUTO -eq 1 ]] && args+=(--auto)
  for d in "${EXTRA_GGUF_DIRS[@]:-}"; do
    [[ -n "$d" ]] && args+=(--gguf-dir "$d")
  done
  "$PY" "$ROOT/scripts/select_models.py" "${args[@]}" || {
    warn "model selection failed/aborted — continuing with existing models.yaml"
  }
else
  printf "${c_dim}[4/6] Skipping model selection (--skip-models)${c_off}\n"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 5) Frontend deps
# ─────────────────────────────────────────────────────────────────────────────
if [[ $SKIP_FRONTEND -eq 0 ]]; then
  if command -v npm >/dev/null 2>&1; then
    step "5/6" "Installing frontend deps (npm)"
    ( cd "$ROOT/frontend" && npm install --prefer-offline 2>&1 | grep -v "^npm warn" || true )
    ok "frontend deps ready"
  else
    warn "npm not found — skipping frontend install. Install Node.js 18+ to use the UI."
  fi
else
  printf "${c_dim}[5/6] Skipping frontend (--skip-frontend)${c_off}\n"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 6) .env from .env.example if missing
# ─────────────────────────────────────────────────────────────────────────────
if [[ ! -f "$ROOT/backend/.env" && -f "$ROOT/backend/.env.example" ]]; then
  cp "$ROOT/backend/.env.example" "$ROOT/backend/.env"
  ok "Wrote backend/.env from .env.example"
else
  printf "${c_dim}[6/6] backend/.env already present.${c_off}\n"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 6b) Initialise SQLite database (auth + EHR + alerts + education content)
# ─────────────────────────────────────────────────────────────────────────────
step "6b" "Initialising local SQLite database"
mkdir -p "$ROOT/backend/data"
( cd "$ROOT/backend" && "$PY" -c "import db; print('  db:', db.get_db_path())" ) \
  || warn "DB init failed"
# Generate a per-installation auth secret if not set yet.
if [[ -f "$ROOT/backend/.env" ]] && ! grep -q '^ASR_AGENT_SECRET=' "$ROOT/backend/.env"; then
  SECRET="$("$PY" -c "import secrets; print(secrets.token_urlsafe(48))")"
  printf "\n# JWT signing secret — generated by setup.sh\nASR_AGENT_SECRET=%s\n" \
    "$SECRET" >> "$ROOT/backend/.env"
  ok "Generated ASR_AGENT_SECRET in backend/.env"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Launch
# ─────────────────────────────────────────────────────────────────────────────
if [[ $NO_START -eq 1 ]]; then
  echo
  printf "${c_grn}Setup complete.${c_off} Start later with:\n"
  echo "  $PY -m uvicorn backend.app:app --port $BACKEND_PORT --reload"
  [[ $SKIP_FRONTEND -eq 0 ]] && echo "  npm --prefix frontend run dev -- -p $FRONTEND_PORT"
  exit 0
fi

say "Launching services"
mkdir -p "$ROOT/.run"

# Backend (FastAPI via uvicorn)
( cd "$ROOT" && "$PY" -m uvicorn backend.app:app \
    --host 127.0.0.1 --port "$BACKEND_PORT" \
    --reload --reload-dir backend ) \
  >"$ROOT/.run/backend.log" 2>&1 &
echo $! > "$ROOT/.run/backend.pid"
ok "Backend  → http://127.0.0.1:${BACKEND_PORT}  (pid $(cat "$ROOT/.run/backend.pid"), log: .run/backend.log)"

# Frontend (Next.js)
if [[ $SKIP_FRONTEND -eq 0 ]] && command -v npm >/dev/null 2>&1 && [[ -d "$ROOT/frontend/node_modules" ]]; then
  ( cd "$ROOT/frontend" && NEXT_PUBLIC_API_URL="http://127.0.0.1:${BACKEND_PORT}" \
      npm run dev -- -p "$FRONTEND_PORT" ) \
    >"$ROOT/.run/frontend.log" 2>&1 &
  echo $! > "$ROOT/.run/frontend.pid"
  ok "Frontend → http://127.0.0.1:${FRONTEND_PORT} (pid $(cat "$ROOT/.run/frontend.pid"), log: .run/frontend.log)"
fi

echo
printf "${c_grn}All services started.${c_off}\n"
echo "  Logs : tail -f '$ROOT/.run/backend.log' '$ROOT/.run/frontend.log'"
echo "  Stop : kill \$(cat '$ROOT/.run/'*.pid)"

