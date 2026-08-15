"""Additive NVIDIA Triton Inference Server ASR client.

Selected via ``models.yaml`` (``provider: triton_asr``). Does NOT modify or
replace ``hf_asr`` / Whisper local transcription logic — that remains the
default fallback when this provider is disabled or Triton is unreachable.

All connection settings come from env / YAML:
  TRITON_URL, TRITON_MODEL, TRITON_TIMEOUT_SEC, etc.

Long-form (>30s) completeness is handled server-side (compat_http_server /
Triton model.py) via WHISPER_CHUNK_LENGTH_S / WHISPER_STRIDE_LENGTH_S.
This client only forwards the full PCM waveform and uses a generous timeout.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np

from .audio_utils import decode_audio_bytes
from .base import ASRProvider

log = logging.getLogger("provider.triton_asr")


class TritonASRProvider(ASRProvider):
    """HTTP/gRPC client for a Whisper (or compatible) model on Triton."""

    kind = "triton_asr"
    role = "asr"

    def __init__(self, *, name: str, config: dict) -> None:
        super().__init__(name=name, config=config)
        self.url = (
            os.getenv("TRITON_URL")
            or config.get("url")
            or config.get("api_base")
            or ""
        ).rstrip("/")
        self.model_name = (
            os.getenv("TRITON_MODEL")
            or config.get("model_name")
            or config.get("model")
            or "whisper"
        )
        self.timeout = float(
            os.getenv("TRITON_TIMEOUT_SEC")
            or config.get("timeout", 300)
        )
        self.target_sr = int(
            os.getenv("WHISPER_TARGET_SR")
            or config.get("target_sr", 16000)
        )
        self.protocol = (
            os.getenv("TRITON_PROTOCOL")
            or config.get("protocol")
            or "http"
        ).lower()
        # Product default: English-only transcripts for report/dictate LLMs.
        # Server should use WHISPER_OUTPUT_ENGLISH=1 / task=translate; this flag
        # adds an LLM post-step if Persian script still appears.
        self.output_english = bool(config.get("output_english", True))
        self._client = None

    async def _load(self) -> None:
        if not self.url:
            raise RuntimeError(
                "TRITON_URL is not set. Configure it in .env / models.yaml "
                "to use the Triton ASR provider."
            )
        # Prefer official tritonclient; fall back to raw httpx for /v2.
        try:
            if self.protocol == "grpc":
                import tritonclient.grpc.aio as grpcclient  # type: ignore

                # grpc URL is host:port without scheme
                host = self.url.replace("http://", "").replace("https://", "")
                self._client = ("grpc", grpcclient.InferenceServerClient(url=host))
            else:
                import tritonclient.http.aio as httpclient  # type: ignore

                host = self.url.replace("http://", "").replace("https://", "")
                self._client = ("http", httpclient.InferenceServerClient(url=host))
            log.info("triton client ready url=%s model=%s", self.url, self.model_name)
        except ImportError:
            log.warning(
                "tritonclient not installed — using raw HTTP /v2 infer at %s",
                self.url,
            )
            self._client = ("raw", None)

    async def health(self) -> dict:
        if not self.url:
            return {"ok": False, "detail": "TRITON_URL unset"}
        try:
            import httpx

            base = self.url if self.url.startswith("http") else f"http://{self.url}"
            # Short connect/read so /api/models stays snappy when Triton is
            # absent. self.timeout is the *inference* budget (often 120s);
            # reusing it here made an unreachable optional provider cost ~2.5s
            # on every registry health call. Mirrors the Ollama provider.
            probe_timeout = httpx.Timeout(
                connect=float(os.getenv("TRITON_HEALTH_CONNECT_TIMEOUT_SEC", "0.5")),
                read=float(os.getenv("TRITON_HEALTH_READ_TIMEOUT_SEC", "2")),
                write=2.0, pool=1.0,
            )
            async with httpx.AsyncClient(timeout=probe_timeout) as c:
                r = await c.get(f"{base}/v2/health/ready")
                ok = r.status_code == 200
            return {
                "ok": ok,
                "url": self.url,
                "model": self.model_name,
                "detail": "ready" if ok else f"HTTP {r.status_code}",
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "url": self.url, "detail": str(e)}

    async def unload(self) -> None:
        self._client = None
        await super().unload()

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        language: Optional[str] = None,
        sample_rate: Optional[int] = None,
        filename_hint: str = "",
    ) -> str:
        await self.ensure_loaded()
        wav, sr = decode_audio_bytes(
            audio_bytes,
            target_sr=self.target_sr,
            filename_hint=filename_hint,
        )
        if sample_rate and int(sample_rate) != self.target_sr:
            # decode_audio_bytes already resamples to target_sr
            pass
        audio = np.asarray(wav, dtype=np.float32).reshape(1, -1)
        duration_s = float(audio.size) / float(self.target_sr)
        log.info(
            "Triton ASR request model=%s duration=%.1fs samples=%d timeout=%.0fs",
            self.model_name,
            duration_s,
            int(audio.size),
            self.timeout,
        )

        kind, client = self._client  # type: ignore[misc]
        if kind == "raw" or client is None:
            text = await self._infer_raw_http(audio, language=language)
        else:
            text = await self._infer_tritonclient(client, kind, audio, language=language)
        if self.output_english and text:
            text = await self._ensure_english(text)
        log.info("Triton ASR done: %d chars (audio was %.1fs)", len(text or ""), duration_s)
        return text

    async def _ensure_english(self, text: str) -> str:
        """If Triton returned Persian script, translate via english_transcript."""
        if not any("\u0600" <= ch <= "\u06FF" for ch in text):
            return text
        try:
            from english_transcript import to_english_clinical
            from registry import Registry

            english = await to_english_clinical(text, registry=Registry.get())
            return english or text
        except Exception as e:  # noqa: BLE001
            log.warning("english_transcript fallback failed: %s", e)
            return text

    async def _infer_raw_http(
        self,
        audio: np.ndarray,
        *,
        language: Optional[str],
    ) -> str:
        import httpx

        base = self.url if self.url.startswith("http") else f"http://{self.url}"
        # OpenAI-style / NVIDIA Riva-compatible envelope is deployment-specific;
        # we use Triton's HTTP v2 JSON (FP32 audio + optional language).
        inputs = [
            {
                "name": "WAV",
                "shape": list(audio.shape),
                "datatype": "FP32",
                "data": audio.flatten().tolist(),
            }
        ]
        if language:
            inputs.append({
                "name": "LANGUAGE",
                "shape": [1],
                "datatype": "BYTES",
                "data": [language],
            })
        body = {
            "inputs": inputs,
            "outputs": [{"name": "TRANSCRIPT"}],
        }
        url = f"{base}/v2/models/{self.model_name}/infer"
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(url, json=body)
            r.raise_for_status()
            data = r.json()
        for out in data.get("outputs", []) or []:
            if out.get("name") == "TRANSCRIPT":
                vals = out.get("data") or []
                if vals:
                    v = vals[0]
                    return v.decode("utf-8") if isinstance(v, (bytes, bytearray)) else str(v)
        raise RuntimeError(f"Triton response missing TRANSCRIPT: {data!r}"[:400])

    async def _infer_tritonclient(
        self,
        client,
        kind: str,
        audio: np.ndarray,
        *,
        language: Optional[str],
    ) -> str:
        if kind == "grpc":
            import tritonclient.grpc.aio as grpcclient  # type: ignore

            inp = grpcclient.InferInput("WAV", list(audio.shape), "FP32")
            inp.set_data_from_numpy(audio)
            inputs = [inp]
            if language:
                lang_inp = grpcclient.InferInput("LANGUAGE", [1], "BYTES")
                lang_inp.set_data_from_numpy(
                    np.array([language.encode("utf-8")], dtype=object)
                )
                inputs.append(lang_inp)
            outs = [grpcclient.InferRequestedOutput("TRANSCRIPT")]
        else:
            import tritonclient.http.aio as httpclient  # type: ignore

            inp = httpclient.InferInput("WAV", list(audio.shape), "FP32")
            inp.set_data_from_numpy(audio)
            inputs = [inp]
            if language:
                lang_inp = httpclient.InferInput("LANGUAGE", [1], "BYTES")
                lang_inp.set_data_from_numpy(
                    np.array([language.encode("utf-8")], dtype=object)
                )
                inputs.append(lang_inp)
            outs = [httpclient.InferRequestedOutput("TRANSCRIPT")]

        result = await client.infer(self.model_name, inputs, outputs=outs)
        arr = result.as_numpy("TRANSCRIPT")
        if arr is None or len(arr) == 0:
            raise RuntimeError("Triton returned empty TRANSCRIPT")
        val = arr[0]
        if isinstance(val, (bytes, bytearray)):
            return val.decode("utf-8")
        return str(val)
