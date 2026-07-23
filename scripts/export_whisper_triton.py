#!/usr/bin/env python3
"""Prepare Whisper artifacts for deploy/triton/model_repository/whisper/.

Production path (matches triton_asr.py WAV→TRANSCRIPT): Triton **Python**
backend — this script verifies model.py is present and that local
whisper-large-v3 weights exist for volume-mount into the container.

Optional ONNX probe: stock Whisper ONNX is encoder/decoder logits only and
does **not** satisfy config I/O (WAV + TRANSCRIPT string). A full large-v3
ONNX e2e graph is not supported here; use the Python backend instead.

Usage (from repo root, prefer project .venv)::

    .\\.venv\\Scripts\\python.exe scripts\\export_whisper_triton.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT / "deploy" / "triton" / "model_repository" / "whisper"
VERSION_DIR = REPO / "1"
LOCAL_WHISPER = ROOT / "models" / "whisper-large-v3"
MODEL_PY = VERSION_DIR / "model.py"
WEIGHTS = LOCAL_WHISPER / "model.safetensors"
EXPECTED_BYTES = 3_087_130_976


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--try-onnx",
        action="store_true",
        help="Attempt optimum ONNX export (documented as incompatible with WAV→TRANSCRIPT).",
    )
    args = ap.parse_args()

    VERSION_DIR.mkdir(parents=True, exist_ok=True)
    if not MODEL_PY.is_file():
        print(f"ERROR: missing Python backend {MODEL_PY}", file=sys.stderr)
        return 1
    print(f"OK python backend: {MODEL_PY}")

    if WEIGHTS.is_file() and WEIGHTS.stat().st_size == EXPECTED_BYTES:
        print(f"OK local whisper-large-v3 weights: {WEIGHTS} ({EXPECTED_BYTES} bytes)")
    elif LOCAL_WHISPER.is_dir():
        print(f"WARN: {LOCAL_WHISPER} present but weights size unexpected")
    else:
        print(
            f"WARN: no local {LOCAL_WHISPER} — container will download "
            "openai/whisper-large-v3 unless WHISPER_MODEL_DIR is mounted"
        )

    cfg = REPO / "config.pbtxt"
    text = cfg.read_text(encoding="utf-8") if cfg.is_file() else ""
    if 'backend: "python"' in text:
        print("OK config.pbtxt uses python backend (WAV->TRANSCRIPT)")
    else:
        print("WARN: config.pbtxt does not declare python backend")

    if args.try_onnx:
        print(
            "\nONNX note: openai/whisper-large-v3 via optimum/transformers.onnx "
            "exports encoder_model.onnx + decoder_model.onnx (mel/logits), NOT a "
            "single model.onnx with WAV input and TRANSCRIPT string output. "
            "Skipping full large-v3 ONNX export (multi-GB, wrong I/O for this "
            "Triton repo). Use the Python backend model.py instead."
        )
        # Tiny marker so operators know ONNX was considered and rejected.
        note = VERSION_DIR / "ONNX_NOT_APPLICABLE.txt"
        note.write_text(
            "Stock Whisper ONNX does not match WAV->TRANSCRIPT. "
            "Serving uses Triton Python backend model.py + whisper-large-v3 weights.\n",
            encoding="utf-8",
        )
        print(f"Wrote {note}")

    print("\nReady for Triton. Example (host 8002 -> container 8000):")
    print(
        "  docker run --gpus all --rm -p 8002:8000 "
        f'-v "{REPO.parent.as_posix()}:/models" '
        f'-v "{LOCAL_WHISPER.as_posix()}:/whisper-weights:ro" '
        '-e WHISPER_MODEL_DIR=/whisper-weights '
        "nvcr.io/nvidia/tritonserver:24.08-py3 "
        "tritonserver --model-repository=/models"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
