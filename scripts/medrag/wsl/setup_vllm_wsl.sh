#!/usr/bin/env bash
# Install CUDA user stack + vLLM inside WSL Ubuntu and optionally start servers.
# Reads knobs from a dotenv file (default: Windows MedicalRAG/.env via /mnt/c/...).
# Usage:
#   bash scripts/wsl/setup_vllm_wsl.sh [--dotenv /path/to/.env] [--serve llm|embed|both|none]
set -euo pipefail

DOTENV=""
SERVE_MODE="none"
VENV_DIR="${HOME}/.venvs/vllm"
HF_CACHE="${HF_HOME:-${HOME}/.cache/huggingface}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dotenv) DOTENV="$2"; shift 2 ;;
    --serve) SERVE_MODE="$2"; shift 2 ;;
    --venv) VENV_DIR="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

load_dotenv() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" != *=* ]] && continue
    local k="${line%%=*}"
    local v="${line#*=}"
    k="$(echo "$k" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    v="$(echo "$v" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;s/^"//;s/"$//;s/^'\''//;s/'\''$//')"
    if [[ -n "$k" && -z "${!k:-}" ]]; then
      export "$k=$v"
    fi
  done < "$f"
}

if [[ -n "$DOTENV" ]]; then
  load_dotenv "$DOTENV"
fi

LLM_MODEL="${VLLM_MODEL:-${MEDRAG_LLM_MODEL:-}}"
LLM_HOST="${VLLM_HOST:-0.0.0.0}"
LLM_PORT="${VLLM_PORT:-8000}"
LLM_MAX_LEN="${VLLM_MAX_MODEL_LEN:-2048}"
# 8GB: free VRAM often ~6.9/8 after Windows — use ≤0.85 + bitsandbytes (see serve_qwen35_8gb.sh)
LLM_MEM="${VLLM_GPU_MEM_UTIL:-0.85}"
LLM_QUANT="${VLLM_QUANTIZATION:-bitsandbytes}"
LLM_LM_ONLY="${VLLM_LANGUAGE_MODEL_ONLY:-1}"

EMBED_MODEL="${VLLM_EMBED_MODEL:-${MEDRAG_EMBED_MODEL:-}}"
EMBED_HOST="${VLLM_EMBED_HOST:-0.0.0.0}"
EMBED_PORT="${VLLM_EMBED_PORT:-8001}"
EMBED_MAX_LEN="${VLLM_EMBED_MAX_MODEL_LEN:-512}"
EMBED_MEM="${VLLM_EMBED_GPU_MEM_UTIL:-0.25}"
EMBED_TASK="${VLLM_EMBED_TASK:-embed}"

echo "==> Distro: $(. /etc/os-release; echo "$PRETTY_NAME")"
echo "==> GPU probe (nvidia-smi):"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv || true
else
  echo "WARNING: nvidia-smi not found in WSL. Install/update NVIDIA Windows Game Ready driver,"
  echo "         then reboot. WSL uses the Windows NVIDIA driver (no separate Linux driver)."
fi

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git curl ca-certificates build-essential

# NVIDIA Container Toolkit is optional (Docker path). Bare-metal vLLM needs CUDA libs.
# Prefer pip wheels that bundle CUDA; install CUDA keyring only if missing libcuda.
if ! ldconfig -p 2>/dev/null | grep -q libcuda; then
  echo "==> Installing CUDA toolkit (repo) for libcuda/runtime stubs if needed..."
  # On WSL2, libcuda.so comes from /usr/lib/wsl/lib via Windows driver.
  if [[ -d /usr/lib/wsl/lib ]]; then
    echo "WSL GPU lib dir present: /usr/lib/wsl/lib"
    echo "/usr/lib/wsl/lib" | sudo tee /etc/ld.so.conf.d/wsl-cuda.conf >/dev/null
    sudo ldconfig || true
  fi
fi

mkdir -p "$(dirname "$VENV_DIR")" "$HF_CACHE"
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install -U pip setuptools wheel

echo "==> Installing latest vLLM (needs NVIDIA driver ~570+/610+ for CUDA 13 wheels)..."
# Qwen3.5 requires recent vLLM; old 0.8.x cannot load qwen3_5.
pip install -U vllm huggingface_hub bitsandbytes ninja
DRIVER_VER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || true)"
echo "Windows/WSL NVIDIA driver: ${DRIVER_VER:-unknown}"

python - <<'PY'
import vllm
print("vllm import OK:", getattr(vllm, "__version__", "?"))
import torch
print("torch:", torch.__version__, "cuda_runtime:", torch.version.cuda,
      "avail:", torch.cuda.is_available(),
      "device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
if not torch.cuda.is_available():
    raise SystemExit("CUDA not available — update Windows NVIDIA driver or check WSL GPU")
try:
    import bitsandbytes as bnb
    print("bitsandbytes:", bnb.__version__)
except Exception as e:
    print("bitsandbytes missing:", e)
PY

export HF_HOME="$HF_CACHE"
export HF_HUB_DISABLE_SYMLINKS_WARNING=1

prefetch() {
  local repo="$1"
  echo "==> Prefetch HF model: $repo"
  python - <<PY
from huggingface_hub import snapshot_download
print(snapshot_download(repo_id="$repo", cache_dir="$HF_CACHE"))
PY
}

[[ -n "$LLM_MODEL" ]] && prefetch "$LLM_MODEL"
[[ -n "$EMBED_MODEL" ]] && prefetch "$EMBED_MODEL"

LOG_DIR="${HOME}/.medrag-vllm-logs"
mkdir -p "$LOG_DIR"

start_llm() {
  [[ -n "$LLM_MODEL" ]] || { echo "Set VLLM_MODEL / MEDRAG_LLM_MODEL"; exit 1; }
  echo "==> vllm serve LLM $LLM_MODEL :$LLM_PORT mem=$LLM_MEM max_len=$LLM_MAX_LEN quant=$LLM_QUANT"
  # Prefer dedicated 8GB helper when present (FlashInfer off, LM-only, bnb)
  local helper
  helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/serve_qwen35_8gb.sh"
  export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"
  if [[ -f "$helper" && "$LLM_QUANT" == "bitsandbytes" ]]; then
    echo "==> Using $helper"
    nohup bash "$helper" >"$LOG_DIR/llm.log" 2>&1 &
  else
    local extra=()
    [[ "$LLM_LM_ONLY" == "1" || "$LLM_LM_ONLY" == "true" ]] && extra+=(--language-model-only)
    if [[ -n "$LLM_QUANT" && "$LLM_QUANT" != "none" ]]; then
      extra+=(--quantization "$LLM_QUANT" --load-format bitsandbytes)
    fi
    nohup vllm serve "$LLM_MODEL" \
      --host "$LLM_HOST" --port "$LLM_PORT" \
      --max-model-len "$LLM_MAX_LEN" \
      --gpu-memory-utilization "$LLM_MEM" \
      --enforce-eager \
      "${extra[@]}" \
      >"$LOG_DIR/llm.log" 2>&1 &
  fi
  echo $! >"$LOG_DIR/llm.pid"
  echo "LLM pid $(cat "$LOG_DIR/llm.pid") log $LOG_DIR/llm.log"
}

start_embed() {
  [[ -n "$EMBED_MODEL" ]] || { echo "Set VLLM_EMBED_MODEL / MEDRAG_EMBED_MODEL"; exit 1; }
  echo "==> vllm serve embed $EMBED_MODEL :$EMBED_PORT mem=$EMBED_MEM max_len=$EMBED_MAX_LEN"
  echo "NOTE: On RTX 3070 8GB, concurrent LLM+embed often OOMs; prefer sequential or CPU embed."
  nohup vllm serve "$EMBED_MODEL" \
    --task "$EMBED_TASK" \
    --host "$EMBED_HOST" --port "$EMBED_PORT" \
    --max-model-len "$EMBED_MAX_LEN" \
    --gpu-memory-utilization "$EMBED_MEM" \
    >"$LOG_DIR/embed.log" 2>&1 &
  echo $! >"$LOG_DIR/embed.pid"
  echo "Embed pid $(cat "$LOG_DIR/embed.pid") log $LOG_DIR/embed.log"
}

case "$SERVE_MODE" in
  llm) start_llm ;;
  embed) start_embed ;;
  both)
    start_llm
    sleep 5
    start_embed
    ;;
  none) echo "==> Install complete (no serve). Use --serve llm|embed|both" ;;
  *) echo "Bad --serve: $SERVE_MODE" >&2; exit 2 ;;
esac

echo "Done."
