"""Shared audio decode helpers for ASR providers."""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

log = logging.getLogger("provider.audio_utils")

# Formats that soundfile reads reliably without ffmpeg.
_NATIVE_EXTS = {".wav", ".flac", ".ogg"}


def decode_audio_bytes(
    buf: bytes,
    *,
    target_sr: int = 16000,
    filename_hint: str = "",
) -> tuple[np.ndarray, int]:
    """Return mono float32 PCM at *target_sr* from arbitrary audio bytes."""
    ext = Path(filename_hint).suffix.lower() if filename_hint else ""
    if ext and ext not in _NATIVE_EXTS:
        return _decode_via_ffmpeg(buf, target_sr=target_sr, ext=ext or ".bin")
    try:
        return _decode_soundfile(buf, target_sr=target_sr)
    except Exception as exc:  # noqa: BLE001
        log.debug("soundfile decode failed (%s); trying ffmpeg", exc)
        return _decode_via_ffmpeg(buf, target_sr=target_sr, ext=ext or ".wav")


def write_temp_wav(wav: np.ndarray, sr: int) -> str:
    """Write mono PCM to a temp WAV file; caller must delete."""
    import soundfile as sf

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    sf.write(path, wav, sr)
    return path


def _decode_soundfile(buf: bytes, *, target_sr: int) -> tuple[np.ndarray, int]:
    import librosa
    import soundfile as sf

    wav, sr = sf.read(io.BytesIO(buf), dtype="float32", always_2d=False)
    if wav.ndim > 1:
        wav = wav.mean(axis=1)
    if sr != target_sr:
        wav = librosa.resample(wav, orig_sr=sr, target_sr=target_sr)
        sr = target_sr
    return wav, sr


def _decode_via_ffmpeg(buf: bytes, *, target_sr: int, ext: str) -> tuple[np.ndarray, int]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            return _decode_via_pyav(buf, target_sr=target_sr)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "ffmpeg is required for m4a/mp3/aac uploads but was not found on "
                "PATH, and the PyAV fallback also failed"
            ) from exc
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as src:
        src.write(buf)
        src_path = src.name
    out_path = src_path + ".wav"
    try:
        cmd = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            src_path,
            "-ac",
            "1",
            "-ar",
            str(target_sr),
            "-f",
            "wav",
            out_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, check=False)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg failed: {err.strip()}")
        return _decode_soundfile(Path(out_path).read_bytes(), target_sr=target_sr)
    finally:
        for p in (src_path, out_path):
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass


def _decode_via_pyav(buf: bytes, *, target_sr: int) -> tuple[np.ndarray, int]:
    """Decode m4a/mp3/aac using PyAV's bundled libav (no ffmpeg binary needed).

    PyAV ships statically linked decoding libraries, so this works in minimal
    containers/deployments where the ``ffmpeg`` CLI isn't installed but the
    ``av`` Python package is (it's already a transitive dependency of
    faster-whisper).
    """
    import av  # type: ignore

    frames: list[np.ndarray] = []
    src_sr = target_sr
    with av.open(io.BytesIO(buf)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=target_sr)
        for frame in container.decode(stream):
            for out_frame in resampler.resample(frame):
                frames.append(out_frame.to_ndarray().reshape(-1))
        src_sr = target_sr
    if not frames:
        raise RuntimeError("PyAV decoded zero audio frames")
    wav = np.concatenate(frames).astype("float32")
    return wav, src_sr
