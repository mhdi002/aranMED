"""Meta's Omnilingual ASR (omniASR-LLM-7B / 3B / 1B / 300M).

This isn't a standard `transformers` model — it ships with its own package
``omnilingual-asr`` that wraps fairseq2.  Install it with::

    pip install omnilingual-asr

Then any ``omniASR_LLM_*`` checkpoint can be referenced by its short id.
Persian is ``pes_Arab`` in the BCP-47-ish code used by the model.

Long-audio support
------------------
The model is capped at 40 s per segment (MAX_ALLOWED_AUDIO_SEC).  For longer
recordings this provider silently splits the audio into overlapping chunks
(default 30 s with 1 s overlap), transcribes each sequentially, then joins
the results with a single space.  Set ``chunk_sec`` / ``overlap_sec`` in the
provider config to tune.
"""
from __future__ import annotations

import io
import logging
import os
import tempfile
import threading
from typing import Optional

import numpy as np

from .base import ASRProvider

log = logging.getLogger("provider.omniasr")

# Model's hard cap (seconds).
_MAX_SEC: float = 38.0  # stay under the 40 s limit with a small margin

# Map ISO-639-1 codes to Omnilingual's `<lang>_<script>` ids.
_LANG_MAP = {
    "fa":  "pes_Arab",
    "en":  "eng_Latn",
    "ar":  "arb_Arab",
    "fr":  "fra_Latn",
    "de":  "deu_Latn",
    "es":  "spa_Latn",
    "tr":  "tur_Latn",
    "zh":  "cmn_Hans",
    "hi":  "hin_Deva",
    "ru":  "rus_Cyrl",
}


class OmniASRProvider(ASRProvider):
    """Wraps :class:`omnilingual_asr.models.inference.pipeline.ASRInferencePipeline`.

    For audio longer than ~38 s the provider automatically splits into
    sequential overlapping chunks, transcribes each, and joins the text.
    """

    kind = "omniasr"

    def __init__(self, *, name: str, config: dict) -> None:
        super().__init__(name=name, config=config)
        # Accept either short id ("omniASR_LLM_7B") or the HF repo id.
        self.model_card = config.get("model_card") or config.get("model_id") or "omniASR_LLM_7B"
        self.default_lang = config.get("default_language", "eng_Latn")
        self.batch_size = int(config.get("batch_size", 1))
        # Chunking: each chunk ≤ chunk_sec, consecutive chunks overlap by
        # overlap_sec so boundary words aren't cut off.
        self.chunk_sec: float = float(config.get("chunk_sec", 30.0))
        self.overlap_sec: float = float(config.get("overlap_sec", 1.0))
        self.force_language = config.get("force_language", False)
        self._pipeline = None
        self._lock = threading.Lock()

    async def _load(self) -> None:
        import torch
        from omnilingual_asr.models.inference.pipeline import ASRInferencePipeline
        log.info("OmniASR loading model_card=%s", self.model_card)
        # Prefer CPU when VRAM is tight; can be overridden via config.
        device_str = self.config.get("device", None)
        if device_str is None:
            try:
                free, total = torch.cuda.mem_get_info()
                # Require at least 3 GB free VRAM; otherwise fall back to CPU.
                device_str = "cuda" if free > 3 * 1024 ** 3 else "cpu"
            except Exception:
                device_str = "cpu"
        import torch as _torch
        device = _torch.device(device_str)
        log.info("OmniASR using device=%s", device)
        self._pipeline = ASRInferencePipeline(
            model_card=self.model_card,
            device=device,
        )

    async def unload(self) -> None:
        import gc
        self._pipeline = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        await super().unload()

    async def health(self) -> dict:
        return {"ok": True, "model_card": self.model_card, "loaded": self._loaded}

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
        return await anyio.to_thread.run_sync(self._transcribe_sync, audio_bytes, language)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _split_into_chunks(
        self, wav: "np.ndarray", sr: int
    ) -> "list[np.ndarray]":
        """Split *wav* into overlapping chunks each ≤ chunk_sec seconds."""
        chunk_samples = int(self.chunk_sec * sr)
        overlap_samples = int(self.overlap_sec * sr)
        step = max(1, chunk_samples - overlap_samples)
        total = len(wav)
        if total <= chunk_samples:
            return [wav]
        chunks: list = []
        start = 0
        while start < total:
            end = min(start + chunk_samples, total)
            chunks.append(wav[start:end])
            if end == total:
                break
            start += step
        log.info(
            "Audio %.1f s split into %d chunks of ≤%.0f s (overlap %.1f s)",
            total / sr, len(chunks), self.chunk_sec, self.overlap_sec,
        )
        return chunks

    def _transcribe_sync(self, audio_bytes: bytes, language: Optional[str]) -> str:
        import soundfile as sf
        buf = io.BytesIO(audio_bytes)
        wav, sr = sf.read(buf, dtype="float32", always_2d=False)
        if wav.ndim > 1:
            wav = wav.mean(axis=1)

        # Only force language if explicitly enabled (for code-switching support)
        if self.force_language and language:
            lang_id = _LANG_MAP.get((language or "").lower(), language) or self.default_lang
        else:
            # Use None to let the model auto-detect language (enables code-switching)
            lang_id = None
        chunks = self._split_into_chunks(wav, sr)
        parts: list[str] = []

        for i, chunk in enumerate(chunks):
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                wav_path = f.name
            try:
                sf.write(wav_path, chunk, sr)
                with self._lock:
                    # If lang_id is None, don't pass lang parameter to enable auto-detection
                    if lang_id:
                        results = self._pipeline.transcribe(  # type: ignore[union-attr]
                            [wav_path], lang=[lang_id], batch_size=self.batch_size
                        )
                    else:
                        results = self._pipeline.transcribe(  # type: ignore[union-attr]
                            [wav_path], batch_size=self.batch_size
                        )
                if results:
                    r = results[0]
                    text = (r.transcription if hasattr(r, "transcription") else str(r)).strip()
                    if text:
                        parts.append(text)
                        log.debug("Chunk %d/%d: %s", i + 1, len(chunks), text)
            finally:
                try:
                    os.unlink(wav_path)
                except OSError:
                    pass

        return " ".join(parts)
