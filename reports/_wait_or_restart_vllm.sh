#!/bin/bash
# Wait for existing vLLM or restart if dead. Keep Ollama unloaded on Windows side.
set -euo pipefail
LOG=/root/.medrag-vllm-logs/llm.log
mkdir -p /root/.medrag-vllm-logs

wait_ready() {
  local i
  for i in $(seq 1 120); do
    if curl -sf -m 3 http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
      echo "vLLM_READY"
      curl -s http://127.0.0.1:8000/v1/models
      return 0
    fi
    if ! pgrep -f 'vllm serve' >/dev/null 2>&1; then
      echo "vLLM_DEAD"
      return 1
    fi
    if (( i % 6 == 0 )); then
      echo "wait_$i"
      tail -3 "$LOG" 2>/dev/null || true
      nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader || true
    fi
    sleep 5
  done
  echo "vLLM_TIMEOUT"
  return 1
}

export VLLM_USE_FLASHINFER_SAMPLER=0
export HF_HUB_DISABLE_SYMLINKS_WARNING=1
HF_CAND="/mnt/c/Users/mhf/Desktop/medical books/MedicalRAG/.cache/huggingface"
if [[ -d "$HF_CAND" ]]; then export HF_HOME="$HF_CAND"; fi
CUDA_HOME_CANDIDATE="/root/.venvs/vllm/lib/python3.12/site-packages/nvidia/cu13"
if [[ -d "$CUDA_HOME_CANDIDATE" ]]; then
  export CUDA_HOME="$CUDA_HOME_CANDIDATE"
  export PATH="$CUDA_HOME/bin:/root/.venvs/vllm/bin:$PATH"
fi

if pgrep -f 'vllm serve' >/dev/null 2>&1; then
  echo "Existing vLLM process found — waiting for ready"
  if wait_ready; then exit 0; fi
  echo "Existing process failed — restarting"
  pkill -9 -f 'vllm serve' || true
  pkill -9 -f 'VLLM::EngineCore' || true
  sleep 2
fi

: > "$LOG"
echo "Starting fresh vLLM"
nohup /root/.venvs/vllm/bin/vllm serve Qwen/Qwen3.5-4B \
  --host 0.0.0.0 --port 8000 \
  --max-model-len 2048 \
  --gpu-memory-utilization 0.55 \
  --enforce-eager \
  --language-model-only \
  --quantization bitsandbytes \
  --load-format bitsandbytes \
  >> "$LOG" 2>&1 &
echo $! > /root/.medrag-vllm-logs/llm.pid
echo "pid=$(cat /root/.medrag-vllm-logs/llm.pid)"
wait_ready
