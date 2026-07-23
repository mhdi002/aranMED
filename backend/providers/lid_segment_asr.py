"""Smart code-switch ASR: 300 ms LID timeline → language spans → per-span VibeVoice.

Phase 1: every *window_ms* (300 ms), run language ID on a short analysis window.
         Compare fa vs en by *relative* score (no absolute confidence threshold,
         no dual transcription pass).

Phase 2: merge consecutive same-language steps into spans
         (e.g. 0–2 s fa, 2–2.3 s en, 2.3–3 s fa …).

Phase 3: transcribe each span once with VibeVoice + --language fa|en|auto.
         Long spans (>max_segment_sec) are split by time only (not by pauses).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field

import numpy as np

from .audio_utils import decode_audio_bytes, write_temp_wav
from .codeswitch import parse_crispasr_stdout

log = logging.getLogger("provider.lid_segment")

# Whisper tiny often labels Persian as ar/he — map to fa for radiology dictation.
_FA_LIKE = frozenset({"fa", "ar", "he", "ur", "pes", "ps", "ku"})
_EN_LIKE = frozenset({"en"})


@dataclass
class LanguageSegment:
    start_sec: float
    end_sec: float
    language: str  # fa | en | auto
    confidence: float = 0.0


@dataclass
class LIDSegmentResult:
    text: str
    raw_text: str
    timeline: list[LanguageSegment] = field(default_factory=list)
    segment_transcripts: list[str] = field(default_factory=list)


class LIDSegmentTranscriber:
    def __init__(
        self,
        *,
        crispasr_binary: str,
        vibevoice_model: str,
        lid_model: str,
        target_sr: int = 16000,
        vibevoice_sr: int = 24000,
        window_ms: float = 300.0,
        lid_analysis_ms: float = 1000.0,
        min_span_sec: float = 0.25,
        min_rms: float = 0.006,
        gpu_device: int = 0,
        max_segment_sec: float = 28.0,
        lang_margin: float = 0.04,
    ) -> None:
        self.crispasr_binary = crispasr_binary
        self.vibevoice_model = vibevoice_model
        self.lid_model = lid_model
        self.target_sr = target_sr
        self.vibevoice_sr = vibevoice_sr
        self.window_ms = window_ms
        self.lid_analysis_ms = lid_analysis_ms
        self.min_span_sec = min_span_sec
        self.min_rms = min_rms
        self.gpu_device = gpu_device
        self.max_segment_sec = max_segment_sec
        self.lang_margin = lang_margin

    @classmethod
    def from_config(cls, config: dict) -> "LIDSegmentTranscriber":
        root = os.path.expanduser("~")
        crisp = config.get(
            "binary_path",
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                ".venv", "CrispASR", "build", "bin", "Release", "crispasr.exe",
            ),
        )
        return cls(
            crispasr_binary=os.path.expanduser(crisp),
            vibevoice_model=os.path.expanduser(config["model_path"]),
            lid_model=os.path.expanduser(
                config.get("lid_model", os.path.join(root, ".cache", "crispasr", "ggml-tiny.bin"))
            ),
            target_sr=int(config.get("target_sr", 16000)),
            vibevoice_sr=int(config.get("vibevoice_sr", 24000)),
            window_ms=float(config.get("window_ms", 300)),
            lid_analysis_ms=float(config.get("lid_analysis_ms", 1000)),
            min_span_sec=float(config.get("min_span_sec", 0.25)),
            gpu_device=int(config.get("gpu_device", 0)),
            max_segment_sec=float(config.get("max_segment_sec", 28)),
            lang_margin=float(config.get("lang_margin", 0.04)),
        )

    async def transcribe(self, audio_bytes: bytes, *, filename_hint: str = "") -> LIDSegmentResult:
        import anyio

        return await anyio.to_thread.run_sync(self._transcribe_sync, audio_bytes, filename_hint)

    def _transcribe_sync(self, audio_bytes: bytes, filename_hint: str) -> LIDSegmentResult:
        import librosa

        wav16, _ = decode_audio_bytes(
            audio_bytes, target_sr=self.target_sr, filename_hint=filename_hint
        )
        timeline = self.build_lid_timeline(wav16, self.target_sr)
        if not timeline:
            timeline = [LanguageSegment(0.0, len(wav16) / self.target_sr, "auto", 0.0)]

        wav24 = librosa.resample(wav16, orig_sr=self.target_sr, target_sr=self.vibevoice_sr)
        parts: list[str] = []
        seg_texts: list[str] = []

        for span in self._split_long_spans(timeline):
            s = int(span.start_sec * self.vibevoice_sr)
            e = int(span.end_sec * self.vibevoice_sr)
            if e <= s:
                continue
            chunk = wav24[s:e]
            text = self._transcribe_span(chunk, span.language)
            if text:
                parts.append(text)
                seg_texts.append(text)
                log.info(
                    "Span %.2f-%.2fs [%s]: %s",
                    span.start_sec, span.end_sec, span.language, text[:100],
                )

        merged = " ".join(parts).strip()
        return LIDSegmentResult(
            text=merged, raw_text=merged, timeline=timeline, segment_transcripts=seg_texts
        )

    def build_lid_timeline(self, wav: np.ndarray, sr: int) -> list[LanguageSegment]:
        """300 ms steps; each step uses a 1 s analysis window for stable LID."""
        hop = max(1, int(self.window_ms / 1000.0 * sr))
        analysis = max(hop, int(self.lid_analysis_ms / 1000.0 * sr))
        n = len(wav)
        steps: list[tuple[float, float, str, float]] = []

        for t in range(0, n, hop):
            a_sec = t / sr
            b_sec = min((t + hop) / sr, n / sr)
            win_start = max(0, t - (analysis - hop) // 2)
            win_end = min(n, win_start + analysis)
            chunk = wav[win_start:win_end]
            if len(chunk) < hop // 2:
                break
            rms = float(np.sqrt(np.mean(chunk ** 2)))
            if rms < self.min_rms:
                continue
            lang, conf = self._detect_language_relative(chunk, sr)
            steps.append((a_sec, b_sec, lang, conf))

        return self._merge_steps(steps)

    def _detect_language_relative(self, chunk: np.ndarray, sr: int) -> tuple[str, float]:
        """Single autodetect per window; map ar/he → fa; en stays en."""
        code, conf = self._lid_autodetect(chunk, sr)
        return self._norm_lang(code), conf

    def _norm_lang(self, code: str) -> str:
        c = (code or "").lower().strip("',\"")
        if c in _EN_LIKE or c.startswith("en"):
            return "en"
        if c in _FA_LIKE or c.startswith("fa"):
            return "fa"
        # Persian code-switch dictation: treat unknown/non-English as fa
        if c and c not in _EN_LIKE:
            return "fa"
        return "auto"

    def _lid_autodetect(self, chunk: np.ndarray, sr: int) -> tuple[str, float]:
        path = write_temp_wav(chunk, sr)
        try:
            cmd = [
                self.crispasr_binary,
                "--backend", "whisper",
                "--model", self.lid_model,
                "--file", path,
                "-dl",
                "-dev", str(self.gpu_device),
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=20)
            blob = (proc.stderr or b"").decode("utf-8", errors="replace")
            m = re.search(r"auto-detected language:\s*(\S+)\s*\(p\s*=\s*([0-9.]+)\)", blob)
            if m:
                return m.group(1), float(m.group(2))
            return "auto", 0.0
        except Exception:  # noqa: BLE001
            return "auto", 0.0
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _merge_steps(
        self, steps: list[tuple[float, float, str, float]]
    ) -> list[LanguageSegment]:
        if not steps:
            return []
        merged: list[LanguageSegment] = []
        cs, ce, cl, confs = steps[0][0], steps[0][1], steps[0][2], [steps[0][3]]
        for a, b, lang, conf in steps[1:]:
            if lang == cl:
                ce = b
                confs.append(conf)
            else:
                merged.append(LanguageSegment(cs, ce, cl, float(np.mean(confs))))
                cs, ce, cl, confs = a, b, lang, [conf]
        merged.append(LanguageSegment(cs, ce, cl, float(np.mean(confs))))

        # Drop ultra-short flicker only when sandwiched by same language on both sides.
        out: list[LanguageSegment] = []
        for i, seg in enumerate(merged):
            dur = seg.end_sec - seg.start_sec
            if (
                dur < self.min_span_sec
                and 0 < i < len(merged) - 1
                and merged[i - 1].language == merged[i + 1].language
            ):
                continue
            out.append(seg)
        return out

    def _split_long_spans(self, timeline: list[LanguageSegment]) -> list[LanguageSegment]:
        """Split long spans by time only (model length limit), keep same language."""
        out: list[LanguageSegment] = []
        for seg in timeline:
            dur = seg.end_sec - seg.start_sec
            if dur <= self.max_segment_sec:
                out.append(seg)
                continue
            n_parts = int(np.ceil(dur / self.max_segment_sec))
            step = dur / n_parts
            for i in range(n_parts):
                out.append(
                    LanguageSegment(
                        seg.start_sec + i * step,
                        seg.start_sec + (i + 1) * step,
                        seg.language,
                        seg.confidence,
                    )
                )
        return out

    def _transcribe_span(self, chunk: np.ndarray, language: str) -> str:
        lang = language if language in ("fa", "en") else "auto"
        path = write_temp_wav(chunk, self.vibevoice_sr)
        try:
            cmd = [
                self.crispasr_binary,
                "--backend", "vibevoice",
                "--model", self.vibevoice_model,
                "--file", path,
                "--language", lang,
                "-oj",
                "-dev", str(self.gpu_device),
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=600)
            if proc.returncode != 0:
                err = (proc.stderr or b"").decode("utf-8", errors="replace")
                log.warning("Span ASR (%s) failed: %s", lang, err[-300:])
                return ""
            out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
            return parse_crispasr_stdout(out) if out else ""
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
