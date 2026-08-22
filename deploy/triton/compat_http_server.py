"""Triton Inference Server HTTP v2 subset for Whisper ASR (local verify).

Used when official ``nvcr.io/nvidia/tritonserver`` cannot be pulled (NGC 403 /
login required). Speaks the same wire protocol as ``backend/providers/triton_asr.py``:

  GET  /v2/health/ready
  POST /v2/models/{model}/infer   inputs: WAV (FP32), optional LANGUAGE
                                  outputs: TRANSCRIPT (BYTES/string)

Loads local ``models/whisper-large-v3`` (or WHISPER_MODEL_DIR / HF id).
Defaults to CPU to avoid fighting MedRAG/vLLM for GPU VRAM.

Long audio (> WHISPER_MAX_SHORTFORM_S, default 30): Whisper's internal long-form
seek often stops mid-file on mixed Persian/English dictation. We therefore run
the ASR pipeline with ``chunk_length_s`` / ``stride_length_s`` (env-configurable)
so the full waveform is covered end-to-end.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEIGHTS = ROOT / "models" / "whisper-large-v3"

log = logging.getLogger("triton.compat")
logging.basicConfig(
    level=os.environ.get("TRITON_COMPAT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(title="triton-http-v2-whisper-compat", version="1.0")
_pipe = None
_target_sr = int(os.environ.get("WHISPER_TARGET_SR", "16000"))
# Match hf_asr output_english=true: Whisper task=translate → English transcript.
# Set WHISPER_OUTPUT_ENGLISH=0 (and WHISPER_TASK=transcribe) to keep source language.
_output_english = os.environ.get("WHISPER_OUTPUT_ENGLISH", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
_task = (
    "translate"
    if _output_english
    else os.environ.get("WHISPER_TASK", "transcribe")
)
_max_shortform_s = float(os.environ.get("WHISPER_MAX_SHORTFORM_S", "30"))
_chunk_length_s = float(os.environ.get("WHISPER_CHUNK_LENGTH_S", "30"))
_stride_length_s = float(os.environ.get("WHISPER_STRIDE_LENGTH_S", "0"))


# The hub id to fall back to when no local snapshot is present. Env-driven
# like every other model reference in the project (docs/core/CONFIGURATION.md)
# so a deployment can serve a different Whisper size without a code change.
HUB_MODEL_ID = os.environ.get("WHISPER_HUB_MODEL_ID", "openai/whisper-large-v3").strip()


def _has_weights(path: Path) -> bool:
    """True when *path* looks like a usable local snapshot.

    A directory alone is not enough: docker-compose bind-mounts
    ``LOCAL_MODELS_DIR`` (default ``./models``) whether or not it contains a
    Whisper snapshot, so the mount point exists and is empty on any host that
    never pre-downloaded one.
    """
    if not path.is_dir():
        return False
    return any(path.glob("*.safetensors")) or any(path.glob("*.bin"))


def _model_id() -> str:
    """Local snapshot when one is really there, else the hub id.

    ``WHISPER_MODEL_DIR`` used to be returned unconditionally, which made the
    fallback below unreachable: compose always sets it, so a host without a
    pre-downloaded snapshot got a hard
    `OSError: Error no file named model.safetensors` and the container
    crash-looped -- even though the weights were sitting in the shared
    hf-cache volume and the hub id would have found them.
    """
    env = (os.environ.get("WHISPER_MODEL_DIR") or os.environ.get("ASR_WHISPER_PATH") or "").strip()
    if env:
        # A hub id (org/name) is not a path -- pass it straight through.
        if "/" in env and not env.startswith(("/", "./", "../")):
            return env
        if _has_weights(Path(env)):
            return env
        log.warning(
            "WHISPER_MODEL_DIR=%s has no model weights; falling back to %s "
            "(resolved from the HF cache if present)", env, HUB_MODEL_ID,
        )
    if _has_weights(DEFAULT_WEIGHTS):
        return str(DEFAULT_WEIGHTS)
    return HUB_MODEL_ID


def _get_pipe():
    global _pipe
    if _pipe is not None:
        return _pipe
    import torch
    from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

    prefer = (os.environ.get("WHISPER_DEVICE") or "cpu").strip().lower()
    use_cuda = prefer.startswith("cuda") and torch.cuda.is_available()
    device = "cuda:0" if use_cuda else "cpu"
    dtype = torch.float16 if use_cuda else torch.float32
    mid = _model_id()
    model = AutoModelForSpeechSeq2Seq.from_pretrained(
        mid, torch_dtype=dtype, low_cpu_mem_usage=True, use_safetensors=True
    )
    model.to(device)
    # Snapshot generation_config may force translate (50360); that conflicts with
    # an explicit task= and can abort long-form seek early. Clear so task wins.
    if getattr(model, "generation_config", None) is not None:
        model.generation_config.forced_decoder_ids = None
    processor = AutoProcessor.from_pretrained(mid)
    _pipe = pipeline(
        "automatic-speech-recognition",
        model=model,
        tokenizer=processor.tokenizer,
        feature_extractor=processor.feature_extractor,
        torch_dtype=dtype,
        device=0 if use_cuda else -1,
    )
    return _pipe


class InferInput(BaseModel):
    name: str
    shape: list[int]
    datatype: str
    data: Any


class InferRequest(BaseModel):
    inputs: list[InferInput]
    outputs: Optional[list[dict]] = None


@app.get("/v2/health/live")
@app.get("/v2/health/ready")
def health_ready():
    return {"ok": True}


@app.get("/v2/models/{model_name}")
def model_meta(model_name: str):
    return {
        "name": model_name,
        "versions": ["1"],
        "platform": "python_compat",
        "inputs": [
            {"name": "WAV", "datatype": "FP32", "shape": [-1]},
            {"name": "LANGUAGE", "datatype": "BYTES", "shape": [1], "optional": True},
        ],
        "outputs": [{"name": "TRANSCRIPT", "datatype": "BYTES", "shape": [1]}],
    }


def _transcribe_full(
    pipe,
    wav: np.ndarray,
    *,
    language: Optional[str],
) -> str:
    """Transcribe entire waveform end-to-end.

    For audio longer than WHISPER_MAX_SHORTFORM_S, Whisper's internal long-form
    seek often stops mid-file on mixed FA/EN dictation, and pipeline
    ``chunk_length_s`` merge is unreliable under concurrent GPU load. We instead
    run short-form inference on consecutive windows and join the texts.
    Overlap (stride) is configurable via WHISPER_STRIDE_LENGTH_S.
    """
    gen_kwargs: dict = {"task": _task}
    if language:
        gen_kwargs["language"] = language

    duration_s = float(wav.size) / float(_target_sr)
    if duration_s <= _max_shortform_s:
        result = pipe(
            {"array": wav, "sampling_rate": _target_sr},
            generate_kwargs=gen_kwargs,
            # Timestamps keep Whisper from stopping mid-utterance on mixed FA/EN.
            return_timestamps=True,
        )
        text = (result.get("text") if isinstance(result, dict) else str(result)) or ""
        return text.strip()

    chunk_n = max(1, int(round(_chunk_length_s * _target_sr)))
    # stride_length_s = overlap; hop = chunk - overlap
    overlap_n = max(0, int(round(_stride_length_s * _target_sr)))
    hop = max(1, chunk_n - overlap_n)
    parts: list[str] = []
    start = 0
    total = int(wav.size)
    while start < total:
        end = min(start + chunk_n, total)
        piece = wav[start:end]
        # Skip tiny trailing fragments (<0.5s) that only add noise
        if piece.size < int(0.5 * _target_sr):
            break
        result = pipe(
            {"array": piece, "sampling_rate": _target_sr},
            generate_kwargs=gen_kwargs,
            return_timestamps=True,
        )
        piece_text = (result.get("text") if isinstance(result, dict) else str(result)) or ""
        piece_text = piece_text.strip()
        if piece_text:
            parts.append(piece_text)
        if end >= total:
            break
        start += hop
    return " ".join(parts).strip()


@app.post("/v2/models/{model_name}/infer")
def infer(model_name: str, body: InferRequest):
    if model_name != (os.environ.get("TRITON_MODEL") or "whisper"):
        # Still allow configured name; default whisper
        pass
    wav = None
    language: Optional[str] = None
    for inp in body.inputs:
        if inp.name == "WAV":
            wav = np.asarray(inp.data, dtype=np.float32).reshape(-1)
        elif inp.name == "LANGUAGE" and inp.data:
            raw = inp.data[0]
            language = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            if not language or language.lower() in ("none", "null"):
                language = None
    if wav is None or wav.size == 0:
        raise HTTPException(400, "missing WAV input")

    pipe = _get_pipe()
    try:
        text = _transcribe_full(pipe, wav, language=language)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"whisper infer failed: {e}") from e
    return {
        "model_name": model_name,
        "model_version": "1",
        "outputs": [
            {
                "name": "TRANSCRIPT",
                "datatype": "BYTES",
                "shape": [1],
                "data": [text],
            }
        ],
    }


def main() -> None:
    host = os.environ.get("TRITON_COMPAT_HOST", "127.0.0.1")
    port = int(os.environ.get("TRITON_COMPAT_PORT", "8002"))
    # Warm model optionally
    if os.environ.get("WHISPER_WARMUP", "1") == "1":
        _get_pipe()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
