"""Split speech on natural pauses (no CrispASR VAD)."""
from __future__ import annotations

import numpy as np


def split_on_pauses(
    wav: np.ndarray,
    sr: int,
    *,
    top_db: float = 26.0,
    min_silence_sec: float = 0.4,
    min_chunk_sec: float = 1.2,
    max_chunk_sec: float = 16.0,
    pad_sec: float = 0.06,
) -> list[tuple[int, int]]:
    """Return sample ranges ``[(start, end), ...]`` for speech-heavy regions."""
    import librosa

    if len(wav) == 0:
        return []

    intervals = librosa.effects.split(wav, top_db=top_db)
    if len(intervals) == 0:
        return [(0, len(wav))]

    pad = int(pad_sec * sr)
    min_sil = int(min_silence_sec * sr)
    min_chunk = int(min_chunk_sec * sr)
    max_chunk = int(max_chunk_sec * sr)

    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        start = max(0, start - pad)
        end = min(len(wav), end + pad)
        if not merged:
            merged.append((start, end))
            continue
        ps, pe = merged[-1]
        if start - pe < min_sil:
            merged[-1] = (ps, max(pe, end))
        else:
            merged.append((start, end))

    out: list[tuple[int, int]] = []
    for start, end in merged:
        if end - start < min_chunk // 3:
            continue
        pos = start
        while pos < end:
            chunk_end = min(pos + max_chunk, end)
            if chunk_end - pos < min_chunk and out:
                out[-1] = (out[-1][0], chunk_end)
            else:
                out.append((pos, chunk_end))
            if chunk_end >= end:
                break
            pos = chunk_end - int(0.15 * sr)  # 150 ms overlap
    return out or [(0, len(wav))]


def script_label(text: str) -> str:
    """Label segment language from transcript script (for timeline only)."""
    fa = sum(1 for c in text if "\u0600" <= c <= "\u06FF")
    en = sum(1 for c in text if c.isascii() and c.isalpha())
    if fa > en * 1.5:
        return "fa"
    if en > fa * 1.5:
        return "en"
    return "mixed"
