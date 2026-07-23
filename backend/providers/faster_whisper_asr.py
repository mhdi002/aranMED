"""Faster-Whisper ASR provider tuned for Persian–English code-switching.

Uses OpenAI Whisper in multilingual auto-detect mode (no forced language) with
VAD segmentation so mixed radiology dictations keep both Persian and English
terms in one transcript.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from .audio_utils import decode_audio_bytes, write_temp_wav
from .base import ASRProvider

log = logging.getLogger("provider.faster_whisper")

# Bilingual radiology context improves English medical terms in FA speech.
_DEFAULT_PROMPT = (
    "Radiology report dictation in Persian and English. "
    "CT MRI ultrasound findings liver spleen kidney pancreas gallbladder "
    "hepatomegaly splenomegaly echogenicity hypoechoic hyperechoic "
    "normal size enlarged no focal lesion کبد طحال کلیه پانکراس "
    "کیسه صفرا افزایش ابعاد افزایش اکوژنیسیته بدون ندول"
)


class FasterWhisperASRProvider(ASRProvider):
    kind = "faster_whisper"

    def __init__(self, *, name: str, config: dict) -> None:
        super().__init__(name=name, config=config)
        self.model_size = config.get("model_size", "large-v3-turbo")
        self.device = config.get("device", "auto")
        self.compute_type = config.get("compute_type", "int8")
        self.target_sr = int(config.get("target_sr", 16000))
        self.beam_size = int(config.get("beam_size", 5))
        self.best_of = int(config.get("best_of", 1))
        self.vad_filter = bool(config.get("vad_filter", True))
        self.force_language = bool(config.get("force_language", False))
        self.initial_prompt = config.get("initial_prompt", _DEFAULT_PROMPT)
        self._model = None
        self._lock = threading.Lock()

    async def _load(self) -> None:
        from faster_whisper import WhisperModel

        device = self._resolve_device()
        compute_type = self._resolve_compute_type(device)
        log.info(
            "FasterWhisper loading %s on %s (%s)",
            self.model_size,
            device,
            compute_type,
        )
        self._model = WhisperModel(
            self.model_size,
            device=device,
            compute_type=compute_type,
        )
        self._device = device

    def _resolve_device(self) -> str:
        if self.device not in ("auto", "", None):
            return self.device
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda"
        except Exception:  # noqa: BLE001
            pass
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
        except Exception:  # noqa: BLE001
            pass
        return "cpu"

    def _resolve_compute_type(self, device: str) -> str:
        compute_type = self.compute_type
        if device == "cuda":
            if compute_type in ("int8", "auto", "", None):
                return "float16"
            return compute_type
        if compute_type in ("float16", "bfloat16"):
            return "int8"
        return compute_type or "int8"

    async def unload(self) -> None:
        self._model = None
        await super().unload()

    async def health(self) -> dict:
        return {
            "ok": True,
            "model_size": self.model_size,
            "device": getattr(self, "_device", self.device),
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

    def _transcribe_sync(
        self, buf: bytes, language: Optional[str], filename_hint: str = ""
    ) -> str:
        wav, sr = decode_audio_bytes(
            buf, target_sr=self.target_sr, filename_hint=filename_hint
        )
        wav_path = write_temp_wav(wav, sr)
        try:
            with self._lock:
                lang = None
                if self.force_language and language:
                    lang = language
                segments, info = self._model.transcribe(  # type: ignore[union-attr]
                    wav_path,
                    language=lang,
                    task="transcribe",
                    beam_size=self.beam_size,
                    best_of=self.best_of,
                    vad_filter=self.vad_filter,
                    initial_prompt=self.initial_prompt or None,
                    condition_on_previous_text=True,
                )
                parts = [seg.text.strip() for seg in segments if seg.text.strip()]
                text = " ".join(parts).strip()
                log.info(
                    "FasterWhisper lang=%s p=%.3f chars=%d",
                    info.language,
                    info.language_probability,
                    len(text),
                )
                return text
        finally:
            import os

            try:
                os.unlink(wav_path)
            except OSError:
                pass
