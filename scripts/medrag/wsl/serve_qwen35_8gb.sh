#!/bin/bash
# Serve Qwen/Qwen3.5-4B on :8000 for RTX 3070 8GB (WSL).
# FP16 weights alone ~8 GiB — use bitsandbytes 4-bit + language-model-only.
set -euo pipefail

source /root/.venvs/vllm/bin/activate

# Prefer project HF cache on Windows drive if present
if [[ -z "${HF_HOME:-}" ]]; then
  if [[ -d "/mnt/c/Users/mhf/Desktop/medical books/MedicalRAG/.cache/huggingface" ]]; then
    export HF_HOME="/mnt/c/Users/mhf/Desktop/medical books/MedicalRAG/.cache/huggingface"
  fi
fi
export HF_HUB_DISABLE_SYMLINKS_WARNING=1

CUDA_HOME_CANDIDATE="/root/.venvs/vllm/lib/python3.12/site-packages/nvidia/cu13"
if [[ -d "$CUDA_HOME_CANDIDATE" ]]; then
  export CUDA_HOME="$CUDA_HOME_CANDIDATE"
  export PATH="$CUDA_HOME/bin:/root/.venvs/vllm/bin:$PATH"
fi
export VLLM_USE_FLASHINFER_SAMPLER=0

MODEL="${VLLM_MODEL:-Qwen/Qwen3.5-4B}"
HOST="${VLLM_HOST:-0.0.0.0}"
PORT="${VLLM_PORT:-8000}"
MAX_LEN="${VLLM_MAX_MODEL_LEN:-2048}"
# Must be <= free/total (Windows often leaves ~6.9/8 GiB free)
MEM_UTIL="${VLLM_GPU_MEM_UTIL:-0.85}"
QUANT="${VLLM_QUANTIZATION:-bitsandbytes}"

mkdir -p /root/.medrag-vllm-logs
nvidia-smi --query-gpu=memory.free,memory.total --format=csv,noheader || true
python -c "import bitsandbytes; print('bnb', bitsandbytes.__version__)"

exec /root/.venvs/vllm/bin/vllm serve "$MODEL" \
  --host "$HOST" \
  --port "$PORT" \
  --max-model-len "$MAX_LEN" \
  --gpu-memory-utilization "$MEM_UTIL" \
  --enforce-eager \
  --language-model-only \
  --quantization "$QUANT" \
  --load-format bitsandbytes \
  2>&1 | tee /root/.medrag-vllm-logs/llm.log
