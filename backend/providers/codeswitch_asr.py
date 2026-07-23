"""VibeVoice code-switch ASR via 300 ms LID timeline (delegates to LIDSegmentTranscriber)."""
from __future__ import annotations

import logging
from typing import Optional

from .base import ASRProvider
from .lid_segment_asr import LIDSegmentTranscriber

log = logging.getLogger("provider.codeswitch_asr")


class CodeSwitchASRProvider(ASRProvider):
    """Smart LID-segment transcription — NOT pause-based chunking."""

    kind = "codeswitch_asr"

    def __init__(self, *, name: str, config: dict) -> None:
        super().__init__(name=name, config=config)
        self.output_english = bool(config.get("output_english", True))
        self.last_raw_transcript = ""
        self.last_timeline: list[dict] = []
        self._engine = LIDSegmentTranscriber.from_config(config)

    async def _load(self) -> None:
        return

    async def health(self) -> dict:
        import os
        ok = os.path.exists(self._engine.vibevoice_model)
        return {"ok": ok, "model_path": self._engine.vibevoice_model, "loaded": True}

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        language: Optional[str] = None,
        sample_rate: Optional[int] = None,
        filename_hint: str = "",
    ) -> str:
        result = await self._engine.transcribe(audio_bytes, filename_hint=filename_hint)
        self.last_raw_transcript = result.raw_text
        self.last_timeline = [
            {
                "start": round(s.start_sec, 2),
                "end": round(s.end_sec, 2),
                "language": s.language,
                "confidence": round(s.confidence, 3),
            }
            for s in result.timeline
        ]
        text = result.text
        if not text or not self.output_english:
            return text
        from english_transcript import to_english_clinical
        from registry import Registry

        return await to_english_clinical(text, registry=Registry.get())
