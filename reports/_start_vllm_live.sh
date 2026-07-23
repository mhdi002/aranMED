set -euo pipefail
source /root/.venvs/vllm/bin/activate
export HF_HUB_DISABLE_SYMLINKS_WARNING=1
export VLLM_USE_FLASHINFER_SAMPLER=0
if [[ -d "/mnt/c/Users/mhf/Desktop/medical books/MedicalRAG/.cache/huggingface" ]]; then
  export HF_HOME="/mnt/c/Users/mhf/Desktop/medical books/MedicalRAG/.cache/huggingface"
fi
CUDA_HOME_CANDIDATE="/root/.venvs/vllm/lib/python3.12/site-packages/nvidia/cu13"
if [[ -d "$CUDA_HOME_CANDIDATE" ]]; then
  export CUDA_HOME="$CUDA_HOME_CANDIDATE"
  export PATH="$CUDA_HOME/bin:/root/.venvs/vllm/bin:$PATH"
fi
MODEL="${VLLM_MODEL:-Qwen/Qwen3.5-4B}"
HOST="${VLLM_HOST:-0.0.0.0}"
PORT="${VLLM_PORT:-8000}"
MAX_LEN="${VLLM_MAX_MODEL_LEN:-2048}"
# Fit beside Triton+embed on 8GB: request ~2.3GiB of total
MEM_UTIL="${VLLM_GPU_MEM_UTIL:-0.28}"
QUANT="${VLLM_QUANTIZATION:-bitsandbytes}"
mkdir -p /root/.medrag-vllm-logs
echo "FREE:"; nvidia-smi --query-gpu=memory.free,memory.used,memory.total --format=csv
echo "Starting vllm serve $MODEL mem=$MEM_UTIL port=$PORT"
nohup /root/.venvs/vllm/bin/vllm serve "$MODEL" \
  --host "$HOST" \
  --port "$PORT" \
  --max-model-len "$MAX_LEN" \
  --gpu-memory-utilization "$MEM_UTIL" \
  --enforce-eager \
  --language-model-only \
  --quantization "$QUANT" \
  --load-format bitsandbytes \
  > /root/.medrag-vllm-logs/llm.log 2>&1 &
echo $! > /root/.medrag-vllm-logs/llm.pid
echo "PID=$(cat /root/.medrag-vllm-logs/llm.pid)"
