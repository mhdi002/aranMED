#!/usr/bin/env bash
# Reinstall vLLM stack compatible with Windows NVIDIA driver 560 (CUDA 12.6).
# Latest vLLM 0.25 pulls torch/cu13 which requires a newer driver.
set -euo pipefail
VENV="${HOME}/.venvs/vllm"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install -U pip setuptools wheel

echo "==> Removing CUDA-13 torch/vLLM if present..."
pip uninstall -y vllm torch torchvision torchaudio 2>/dev/null || true

echo "==> Installing PyTorch cu124..."
pip install torch==2.6.0 torchvision==0.21.0 torchaudio==2.6.0 \
  --index-url https://download.pytorch.org/whl/cu124

python - <<'PY'
import torch
print("torch", torch.__version__, "cuda", torch.version.cuda,
      "avail", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device", torch.cuda.get_device_name(0))
else:
    raise SystemExit("CUDA not available with cu124 torch")
PY

echo "==> Installing vLLM 0.8.5 (matches torch 2.6 / CUDA 12.4)..."
# --no-deps first then add deps carefully? Prefer normal install and let it
# pull compatible deps; if it upgrades torch, re-pin after.
pip install "vllm==0.8.5" huggingface_hub
# Ensure torch stayed on cu124
pip install --force-reinstall --no-deps torch==2.6.0 torchvision==0.21.0 torchaudio==2.6.0 \
  --index-url https://download.pytorch.org/whl/cu124

python - <<'PY'
import vllm, torch
print("vllm", getattr(vllm, "__version__", "?"))
print("torch", torch.__version__, "cuda", torch.version.cuda,
      "avail", torch.cuda.is_available(),
      "device", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
assert torch.cuda.is_available(), "CUDA required"
PY

echo "CU124_VLLM_OK"
