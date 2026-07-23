"""HuggingFace Whisper ASR — openai/whisper-large-v3 with code-switching support."""
from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from .audio_utils import decode_audio_bytes
from .base import ASRProvider

log = logging.getLogger("provider.hf_asr")

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _BACKEND_ROOT.parent

_RAD_PROMPT = (
    "Radiology ultrasound dictation. Patient name. "
    "Abdomen pelvis sonography. Liver spleen gallbladder pancreas CBD. "
    "Echogenicity hypoechoic hyperechoic hepatomegaly splenomegaly."
)


def _fix_patient_role(text: str) -> str:
    """Whisper translate often turns 'patient X' into 'I am Dr. X'."""
    text = re.sub(
        r"(?i)^(?:hello,?\s*)?i am dr\.?\s+([^\.]+)\.\s*i have\b",
        r"The patient is \1. The patient has",
        text,
        count=1,
    )
    text = re.sub(
        r"(?i)^(?:hello,?\s*)?i am dr\.?\s+([^\.]+)\.",
        r"The patient is \1.",
        text,
        count=1,
    )
    text = re.sub(
        r"(?i)^(?:hello,?\s*)?dr\.?\s+([^\.]+?)\s+has\b",
        r"The patient \1 has",
        text,
        count=1,
    )
    text = re.sub(
        r"(?i)(the patient is [^,\.]+), a sonography doctor",
        r"\1 has abdomen pelvis sonography",
        text,
        count=1,
    )
    return text


def _trim_tail_repetition(text: str) -> str:
    """Drop hallucinated trailing loops such as repeated 'Thank you.'."""
    text = re.sub(r"(?:\s*Thank you\.){3,}\s*$", ".", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:\s*Thank you\.){2}\s*$", ".", text, flags=re.IGNORECASE)
    return text.strip()


def _resolve_device(pref: str) -> str:
    import torch

    if pref == "auto":
        return "cuda:0" if torch.cuda.is_available() else "cpu"
    if pref == "cuda" and torch.cuda.is_available():
        return "cuda:0"
    return pref


def _resolve_dtype(name: str, device: str):
    import torch

    if device.startswith("cpu"):
        return torch.float32
    return {"bfloat16": torch.bfloat16, "float16": torch.float16,
            "float32": torch.float32}.get(name, torch.float16)


def _resolve_model_source(model_id: str) -> tuple[str, bool]:
    """Return (load_source, local_files_only).

    If a local snapshot exists under models/<repo-name>, use it directly so
    no network call to the HF hub is needed. Otherwise fall back to the hub
    repo id (requires network access to download/verify).
    """
    if model_id.startswith(("./", "../")) or Path(model_id).is_absolute():
        p = Path(model_id)
        if not p.is_absolute():
            p = (_PROJECT_ROOT / p).resolve()
        return str(p), p.is_dir()

    local_dir = _PROJECT_ROOT / "models" / model_id.split("/")[-1]
    if local_dir.is_dir() and (local_dir / "config.json").exists():
        return str(local_dir), True

    return model_id, False


class HFASRProvider(ASRProvider):
    kind = "hf_asr"

    def __init__(self, *, name: str, config: dict) -> None:
        super().__init__(name=name, config=config)
        self.model_id = config["model_id"]
        self.device_pref = config.get("device", "auto")
        self.dtype_name = config.get("dtype", "float16")
        self.target_sr = int(config.get("target_sr", 16000))
        self.force_language = bool(config.get("force_language", False))
        self.task = config.get("task", "transcribe")
        self.output_english = bool(config.get("output_english", False))
        self.initial_prompt = config.get("initial_prompt", _RAD_PROMPT)
        self.last_raw_transcript = ""
        # Long-form limits (env/config — no hardcoding of production cutoffs).
        self.max_shortform_s = float(
            config.get("max_shortform_s")
            or os.getenv("WHISPER_MAX_SHORTFORM_S", "30")
        )
        self.chunk_length_s = float(
            config.get("chunk_length_s")
            or os.getenv("WHISPER_CHUNK_LENGTH_S", "30")
        )
        self.stride_length_s = float(
            config.get("stride_length_s")
            or os.getenv("WHISPER_STRIDE_LENGTH_S", "0")
        )
        self._model = None
        self._processor = None
        self._dtype = None
        self._lock = threading.Lock()

    async def _load(self) -> None:
        import torch
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

        dev = _resolve_device(self.device_pref)
        dt = _resolve_dtype(self.dtype_name, dev)
        model_source, local_only = _resolve_model_source(self.model_id)
        log.info("Loading Whisper %s on %s (%s)", model_source, dev, dt)

        model = AutoModelForSpeechSeq2Seq.from_pretrained(
            model_source,
            torch_dtype=dt,
            low_cpu_mem_usage=True,
            use_safetensors=True,
            local_files_only=local_only,
        )
        processor = AutoProcessor.from_pretrained(
            model_source,
            local_files_only=local_only,
        )

        self._model = model.to(dev)
        self._processor = processor
        self._dtype = dt
        self._device = dev
        # Avoid conflict between snapshot forced_decoder_ids (often translate)
        # and explicit task= in generate_kwargs — can truncate long-form seek.
        if getattr(self._model, "generation_config", None) is not None:
            self._model.generation_config.forced_decoder_ids = None

    async def unload(self) -> None:
        import gc

        self._model = None
        self._processor = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        await super().unload()

    async def health(self) -> dict:
        return {
            "ok": True,
            "model_id": self.model_id,
            "device": getattr(self, "_device", self.device_pref),
            "loaded": self._loaded,
        }

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        language: Optional[str] = None,
        sample_rate: Optional[int] = None,
        filename_hint: str = "",
    ) -> str:
        await self.ensure_loaded()
        import anyio

        return await anyio.to_thread.run_sync(
            self._transcribe_sync, audio_bytes, language, filename_hint
        )

    def _build_generate_kwargs(self, language: Optional[str], *, long_form: bool) -> dict:
        task = "translate" if self.output_english else self.task
        gen: dict = {"task": task}
        if long_form:
            gen["condition_on_prev_tokens"] = True
        if self.initial_prompt and not long_form:
            prompt_ids = self._processor.get_prompt_ids(self.initial_prompt, return_tensors="pt")
            gen["prompt_ids"] = prompt_ids.to(self._device)
        if self.force_language and language:
            lang_map = {"fa": "persian", "en": "english", "pes": "persian"}
            gen["language"] = lang_map.get(language.lower(), language)
        return gen

    def _to_device(self, features):
        import torch

        out = {}
        for key, value in features.items():
            if not hasattr(value, "to"):
                out[key] = value
                continue
            value = value.to(self._device)
            if self._dtype is not None and value.is_floating_point():
                value = value.to(self._dtype)
            out[key] = value
        return out

    def _generate_long_form(self, wav: np.ndarray, *, task: str, language: Optional[str]) -> str:
        """Full-file transcription via consecutive short-form windows.

        Whisper's internal long-form seek can stop mid-file on mixed FA/EN
        dictation. Short-form windows of ``chunk_length_s`` with
        ``stride_length_s`` overlap are joined instead. Short-form path in
        ``_generate`` is unchanged.
        """
        import torch

        gen_kwargs = self._build_generate_kwargs(language, long_form=False)
        gen_kwargs["task"] = task
        # Mid-file windows must not reuse the opening radiology prompt — it
        # biases later chunks and can truncate real content.
        gen_kwargs.pop("prompt_ids", None)
        # Without timestamp tokens Whisper often stops mid-window on mixed FA/EN.
        gen_kwargs["return_timestamps"] = True
        chunk_n = max(1, int(round(self.chunk_length_s * self.target_sr)))
        overlap_n = max(0, int(round(self.stride_length_s * self.target_sr)))
        hop = max(1, chunk_n - overlap_n)
        parts: list[str] = []
        start = 0
        total = len(wav)
        time_precision = (
            self._processor.feature_extractor.chunk_length
            / self._model.config.max_source_positions
        )
        while start < total:
            end = min(start + chunk_n, total)
            piece = wav[start:end]
            if len(piece) < int(0.5 * self.target_sr):
                break
            features = self._processor(
                piece,
                sampling_rate=self.target_sr,
                return_tensors="pt",
                return_attention_mask=True,
            )
            features = self._to_device(features)
            with torch.inference_mode():
                output = self._model.generate(**features, **gen_kwargs)
            ids = output["sequences"] if isinstance(output, dict) else output
            model_outputs = [{"tokens": ids[i : i + 1]} for i in range(ids.shape[0])]
            piece_text, _optional = self._processor.tokenizer._decode_asr(
                model_outputs,
                return_timestamps=True,
                return_language=False,
                time_precision=time_precision,
            )
            piece_text = (piece_text or "").strip()
            if piece_text:
                parts.append(piece_text)
            if end >= total:
                break
            start += hop
        return " ".join(parts).strip()

    def _generate(self, wav: np.ndarray, *, task: str, language: Optional[str]) -> str:
        import torch

        long_form = len(wav) > self.max_shortform_s * self.target_sr
        if long_form:
            return self._generate_long_form(wav, task=task, language=language)

        gen_kwargs = self._build_generate_kwargs(language, long_form=False)
        gen_kwargs["task"] = task

        features = self._processor(
            wav,
            sampling_rate=self.target_sr,
            return_tensors="pt",
            return_attention_mask=True,
        )
        features = self._to_device(features)

        with torch.inference_mode():
            output = self._model.generate(**features, **gen_kwargs)

        token_ids = output["sequences"] if isinstance(output, dict) else output
        return self._processor.batch_decode(
            token_ids,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )[0].strip()

    def _transcribe_sync(
        self, buf: bytes, language: Optional[str], filename_hint: str = ""
    ) -> str:
        with self._lock:
            wav, sr = decode_audio_bytes(
                buf, target_sr=self.target_sr, filename_hint=filename_hint
            )
            wav = wav.astype(np.float32)
            duration_s = len(wav) / sr

            task = "translate" if self.output_english else self.task
            log.info(
                "Whisper transcribe task=%s force_lang=%s len=%.1fs",
                task,
                self.force_language,
                duration_s,
            )

            text = self._generate(wav, task=task, language=language)
            if self.output_english:
                text = _fix_patient_role(text)
                text = _trim_tail_repetition(text)
            self.last_raw_transcript = text
            log.info("Whisper done: %d chars", len(text))
            return text
