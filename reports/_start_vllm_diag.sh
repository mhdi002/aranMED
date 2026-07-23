#!/bin/bash
set -euo pipefail
source /root/.venvs/vllm/bin/activate
export VLLM_USE_FLASHINFER_SAMPLER=0
export HF_HUB_DISABLE_SYMLINKS_WARNING=1
HF_CAND="/mnt/c/Users/mhf/Desktop/medical books/MedicalRAG/.cache/huggingface"
if [[ -d "$HF_CAND" ]]; then
  export HF_HOME="$HF_CAND"
fi
CUDA_HOME_CANDIDATE="/root/.venvs/vllm/lib/python3.12/site-packages/nvidia/cu13"
if [[ -d "$CUDA_HOME_CANDIDATE" ]]; then
  export CUDA_HOME="$CUDA_HOME_CANDIDATE"
  export PATH="$CUDA_HOME/bin:/root/.venvs/vllm/bin:$PATH"
fi
echo "=== imports ==="
python -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)"
python -c "import bitsandbytes as bnb; print('bnb', bnb.__version__)"
python -c "import vllm; print('vllm', getattr(vllm,'__version__','?'))"
which vllm
nvidia-smi --query-gpu=memory.free,memory.used --format=csv
echo "=== starting vllm (120s timeout) ==="
set +e
timeout 120 /root/.venvs/vllm/bin/vllm serve Qwen/Qwen3.5-4B \
  --host 127.0.0.1 --port 8000 \
  --max-model-len 1024 \
  --gpu-memory-utilization 0.55 \
  --enforce-eager \
  --language-model-only \
  --quantization bitsandbytes \
  --load-format bitsandbytes \
  > /tmp/vllm_try.log 2>&1
echo EXIT:$?
wc -l /tmp/vllm_try.log
tail -100 /tmp/vllm_try.log
