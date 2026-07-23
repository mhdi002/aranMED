#!/usr/bin/env bash
# Entrypoint for Triton Whisper (Python backend). Installs HF deps then starts server.
set -euo pipefail
python3 -m pip install --quiet --no-cache-dir \
  "transformers>=4.40" "accelerate" "safetensors" "soundfile" "sentencepiece" || true
# Prefer torch already in image; install CPU/GPU torch if missing.
python3 -c "import torch" 2>/dev/null || \
  python3 -m pip install --quiet --no-cache-dir "torch" --index-url https://download.pytorch.org/whl/cu124 || \
  python3 -m pip install --quiet --no-cache-dir "torch"
exec tritonserver --model-repository=/models --http-port=8000 --grpc-port=8001 --metrics-port=8002
