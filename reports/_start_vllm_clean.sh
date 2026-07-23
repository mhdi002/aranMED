#!/bin/bash
set -euo pipefail
source /root/.venvs/vllm/bin/activate
export VLLM_USE_FLASHINFER_SAMPLER=0
export HF_HUB_DISABLE_SYMLINKS_WARNING=1
HF_CAND="/mnt/c/Users/mhf/Desktop/medical books/MedicalRAG/.cache/huggingface"
if [[ -d "$HF_CAND" ]]; then export HF_HOME="$HF_CAND"; fi
CUDA_HOME_CANDIDATE="/root/.venvs/vllm/lib/python3.12/site-packages/nvidia/cu13"
if [[ -d "$CUDA_HOME_CANDIDATE" ]]; then
  export CUDA_HOME="$CUDA_HOME_CANDIDATE"
  export PATH="$CUDA_HOME/bin:/root/.venvs/vllm/bin:$PATH"
fi
# ensure only one instance
pkill -9 -f 'vllm serve' 2>/dev/null || true
pkill -9 -f 'VLLM::EngineCore' 2>/dev/null || true
sleep 2
mkdir -p /root/.medrag-vllm-logs
: > /root/.medrag-vllm-logs/llm.log
nvidia-smi --query-gpu=memory.free,memory.used --format=csv
echo "Starting single vllm serve..."
nohup /root/.venvs/vllm/bin/vllm serve Qwen/Qwen3.5-4B \
  --host 0.0.0.0 --port 8000 \
  --max-model-len 2048 \
  --gpu-memory-utilization 0.55 \
  --enforce-eager \
  --language-model-only \
  --quantization bitsandbytes \
  --load-format bitsandbytes \
  >> /root/.medrag-vllm-logs/llm.log 2>&1 &
echo $! > /root/.medrag-vllm-logs/llm.pid
echo "pid=$(cat /root/.medrag-vllm-logs/llm.pid)"
