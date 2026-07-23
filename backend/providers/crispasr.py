"""CrispASR (GGUF) ASR provider."""
from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import tempfile
from typing import Optional

import numpy as np

from .base import ASRProvider

log = logging.getLogger("provider.crispasr")


class CrispASRProvider(ASRProvider):
    """ASR provider using the CrispASR C++ binary for GGUF models.

    Specifically handles Microsoft's VibeVoice-ASR which requires 24kHz mono.
    """

    kind = "crispasr"

    def __init__(self, *, name: str, config: dict) -> None:
        super().__init__(name=name, config=config)
        self.model_path = os.path.expanduser(config["model_path"])
        self.binary_path = config.get("binary_path", "crispasr")
        self.target_sr = int(config.get("target_sr", 24000))
        self.backend = config.get("backend", "vibevoice")
        self.extra_args = config.get("extra_args", [])
        self.force_language = config.get("force_language", False)
        # "auto" keeps multilingual decoding; do not let LID lock to one language.
        self.language_mode = config.get("language_mode", "auto")

    async def health(self) -> dict:
        exists = os.path.exists(self.model_path)
        return {"ok": exists, "model_path": self.model_path}

    def _decode(self, buf: bytes, filename_hint: str = "") -> tuple[np.ndarray, int]:
        from .audio_utils import decode_audio_bytes

        return decode_audio_bytes(
            buf, target_sr=self.target_sr, filename_hint=filename_hint
        )

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        language: Optional[str] = None,
        sample_rate: Optional[int] = None,
        filename_hint: str = "",
    ) -> str:
        import anyio
        return await anyio.to_thread.run_sync(
            self._transcribe_sync, audio_bytes, language, filename_hint
        )

    def _transcribe_sync(
        self, buf: bytes, language: Optional[str], filename_hint: str = ""
    ) -> str:
        import soundfile as sf
        wav, sr = self._decode(buf, filename_hint)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name

        try:
            sf.write(wav_path, wav, sr)
            cmd = [
                self.binary_path,
                "--model", self.model_path,
                "--file", wav_path,
                "--backend", self.backend,
            ]
            if self.force_language and language:
                cmd.extend(["--language", language])
            elif not self.force_language:
                # Multilingual / code-switching: never pin LID to a single lang.
                cmd.extend(["--language", self.language_mode or "auto"])
            cmd.extend(self.extra_args)

            log.info("Running CrispASR: %s", " ".join(cmd))

            # Run subprocess with timeout (e.g., 60 seconds)
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,           # do not raise on non-zero exit
                timeout=300,
            )

            # Decode stdout/stderr as UTF-8
            stdout = result.stdout.decode("utf-8", errors="replace").strip()
            stderr = result.stderr.decode("utf-8", errors="replace").strip()

            log.debug("ASR stdout (first 500 chars): %s", stdout[:500])
            if stderr:
                log.debug("ASR stderr: %s", stderr[:500])

            if result.returncode != 0:
                log.error("CrispASR exited with code %d: %s", result.returncode, stderr)
                # Fallback: return empty string (will cause 500 later, but we log)
                return ""

            if not stdout:
                log.warning("ASR produced empty stdout. stderr: %s", stderr)
                return ""

            # Clean stray trailing characters
            if stdout.endswith('].'):
                stdout = stdout[:-1]
            stdout = stdout.rstrip('.').strip()

            # Parse JSON (may contain multiple passes from VibeVoice slices)
            try:
                from .codeswitch import pick_best_transcript

                return pick_best_transcript(stdout)
            except Exception:  # noqa: BLE001
                pass
            try:
                data = json.loads(stdout)
                if isinstance(data, list):
                    texts = [seg.get("Content", "") for seg in data if "Content" in seg]
                    result_text = " ".join(texts).strip()
                    log.info("ASR transcription: %s", result_text[:100])
                    return result_text
                elif isinstance(data, dict):
                    return data.get("text", data.get("Content", "")).strip()
                else:
                    return stdout
            except json.JSONDecodeError as e:
                log.error("JSON decode error: %s\nRaw output: %s", e, stdout[:500])
                # Return raw output as fallback
                return stdout

        except subprocess.TimeoutExpired:
            log.error("CrispASR timed out after 60 seconds")
            return ""
        except Exception as e:
            log.exception("Unexpected error in CrispASR provider")
            return ""
        finally:
            if os.path.exists(wav_path):
                os.unlink(wav_path)