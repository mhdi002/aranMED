"""Helpers for Persian–English code-switching ASR post-processing."""
from __future__ import annotations

import json
import re
from typing import Iterable

# Common radiology terms in both languages (lowercase match).
_MEDICAL_HINTS = (
    "liver", "spleen", "kidney", "pancreas", "gallbladder", "hepat",
    "splen", "renal", "pelvis", "abdomen", "echogenic", "hypoecho",
    "hyperecho", "enlarged", "normal size", "no focal", "ct ", "mri ",
    "کبد", "طحال", "کلیه", "پانکراس", "کیسه صفرا", "اکو", "ابعاد",
    "افزایش", "ندول", "توده", "کورت", "پلاک", "اپتوپل", "سونو",
)

_GIBBERISH_HINTS = (
    "equal gender", "parent size", "manual mirror", "both getting her",
    "period of your collection", "eco gender city", "bef ops",
)

_CONTENT_RE = re.compile(r'"Content"\s*:\s*"((?:\\.|[^"\\])*)"', re.DOTALL)
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")


def extract_json_arrays(raw: str) -> list[list[dict]]:
    """Pull every JSON array CrispASR may emit (multi-pass / multi-slice)."""
    raw = raw.strip()
    if raw.endswith("]."):
        raw = raw[:-1]
    arrays: list[list[dict]] = []

    # Fast path: whole stdout is one array.
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [data]
    except json.JSONDecodeError:
        pass

    # CrispASR sometimes prints: `[{...}]. [{...}].`
    for chunk in re.split(r"\]\s*\.\s*\[", raw):
        chunk = chunk.strip()
        if not chunk.startswith("["):
            chunk = "[" + chunk
        if not chunk.endswith("]"):
            chunk = chunk + "]"
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            arrays.append(data)
    return arrays


def segments_to_text(segments: Iterable[dict]) -> str:
    parts: list[str] = []
    for seg in segments:
        text = (seg.get("Content") or seg.get("content") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def score_codeswitch_text(text: str) -> float:
    """Higher = better bilingual radiology transcript."""
    if not text.strip():
        return -1.0
    lower = text.lower()
    persian = len(re.findall(r"[\u0600-\u06FF]", text))
    latin_words = len(re.findall(r"[A-Za-z]{3,}", text))
    medical = sum(1 for hint in _MEDICAL_HINTS if hint in lower or hint in text)
    gibberish = sum(2 for hint in _GIBBERISH_HINTS if hint in lower)
    # Reward mixed script; penalise obvious ASR hallucinations.
    mixed_bonus = 8.0 if persian > 10 and latin_words > 3 else 0.0
    return persian * 1.5 + latin_words + medical * 6.0 + mixed_bonus - gibberish * 4.0


def clean_asr_hallucination(text: str) -> str:
    """Drop CJK loops and runaway repetitions common in VibeVoice auto mode."""
    if not text:
        return ""
    text = _CJK_RE.sub("", text)
    # Truncate at runaway repetition (same 2+ char unit repeated many times).
    m = re.search(r"(.{2,12}?)(?:\1){8,}", text)
    if m:
        text = text[: m.start()].strip()
    # Strip embedded JSON artefacts
    text = re.sub(r'\[\{"STart".*?"Content":"', " ", text)
    text = re.sub(r'"\}\]', " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_crispasr_stdout(raw: str) -> str:
    """Extract spoken text from CrispASR JSON stdout."""
    raw = raw.strip()
    arrays = extract_json_arrays(raw)
    candidates: list[str] = []
    for arr in arrays:
        t = segments_to_text(arr)
        if t:
            candidates.append(t)
    if not candidates:
        decoded = []
        for m in _CONTENT_RE.findall(raw):
            decoded.append(bytes(m, "utf-8").decode("unicode_escape", errors="replace"))
        if decoded:
            candidates.append(" ".join(decoded))
    if not candidates:
        # Last resort: text after last Content marker
        tail = re.sub(r"^.*?\}\]\.?\s*", "", raw, flags=re.DOTALL)
        if tail and tail != raw:
            candidates.append(tail)
        else:
            candidates.append(raw)
    best = max(candidates, key=score_codeswitch_text)
    return clean_asr_hallucination(best)


def pick_best_transcript(raw_stdout: str) -> str:
    """Choose the best pass when CrispASR returns multiple JSON arrays."""
    return parse_crispasr_stdout(raw_stdout)


def merge_segment_arrays(arrays: list[list[dict]]) -> str:
    """Merge timed segments from all passes, de-duplicating overlaps."""
    seen: set[str] = set()
    merged: list[str] = []
    for arr in sorted(arrays, key=lambda a: -score_codeswitch_text(segments_to_text(a))):
        for seg in arr:
            text = (seg.get("Content") or seg.get("content") or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
    return " ".join(merged).strip()
