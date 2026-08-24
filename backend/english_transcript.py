"""Translate bilingual ASR output into English clinical dictation.

This module is the second stage of `/api/transcribe`: Whisper produces Persian,
and this turns it into English. It is also where a clinical-safety defect lived,
so the design is deliberately defensive.

Given the whole transcript as one blob, a small model asked to "translate
radiology dictation" does not translate — it *writes a radiology report*. On a
real 41.9 s sample it invented the anatomy (a portal vein the source never
mentions), invented negative findings ("no thrombus", "no focal lesions"),
invented a measurement, and inverted a negation. Three runs of identical input
at temperature 0.1 gave three mutually incompatible clinical readings, each
fluent enough to pass as a better transcript than the truth.

Two mechanisms address that, because prompting alone demonstrably did not: the
previous prompt already said "Preserve every finding, organ, measurement, and
qualifier" and "Do NOT invent findings not present in the source", and the model
disregarded both.

1. SEGMENTED translation. Short segments are translated independently, so the
   model never holds enough context to compose a plausible whole-study
   narrative. It can only render what is in front of it.
2. NUMERAL verification. Every numeral in the source must appear in the output,
   and any numeral in the output that is not in the source is a fabrication.
   Measurements are the part a clinician cannot re-derive from context, which
   makes them both the highest-stakes content and a cheap, objective check.

On failure the result degrades to the faithful source text rather than to
confident fiction. A transcript a clinician must read in Persian is worse
usability and better medicine.

See docs/core/ASR_TRANSLATION_CONFABULATION.md for the full evidence.
"""
from __future__ import annotations

import logging
import os
import re

from providers.base import ChatMessage

log = logging.getLogger("english_transcript")

# Segment-at-a-time, so the model cannot invent a narrative across findings.
ENGLISH_DICTATION_SYS = """You translate ONE SHORT FRAGMENT of Persian/English radiology dictation into clinical English.

You are a translator, not a radiologist. You are rendering a fragment, not writing a report.

RULES:
1. Translate Persian into accurate clinical English. Keep English phrases as they are.
2. Fix only obvious ASR misspellings of clinical terms (echogenicity, hypoechoic, splenomegaly, hepatomegaly, pancreas, gallbladder, cholelithiasis, hydronephrosis).
3. Copy EVERY number exactly as it appears. Never round, merge, drop, or add a number. "50 در 50 در 50" is "50 x 50 x 50".
4. Preserve negation. "نه" / "no" / "بدون" means the finding is ABSENT — never render an absent finding as present, or a present one as absent.
5. Add NOTHING. Do not add findings, organs, measurements, normal statements, or reassurances. Never write phrases like "no thrombus was identified" or "no focal lesions are seen" unless the fragment itself says so. Inventing a negative finding is as dangerous as inventing a positive one.
6. If a fragment is garbled or you cannot tell what it means, transliterate it as literally as you can. Do NOT guess at a plausible clinical meaning.
7. Output ONLY the translated fragment. No headings, no markdown, no commentary, no added sentences.
"""

# Persian/Arabic-Indic digits, so a numeral written in either script compares.
_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# Split on sentence/clause boundaries in either script.
_BOUNDARY = re.compile(r"(?<=[.!?؟۔؛،;،])\s+|\n+")


def _as_bool(value: str | None, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _has_persian(text: str) -> bool:
    return any("؀" <= ch <= "ۿ" for ch in text)


def numerals(text: str) -> list[str]:
    """Every number in `text`, with Persian/Arabic-Indic digits normalised."""
    return re.findall(r"\d+", (text or "").translate(_DIGIT_MAP))


def segment(text: str, max_chars: int) -> list[str]:
    """Split into fragments of at most ~max_chars, preferring clause breaks.

    Fragments are what bound the model's freedom to invent, so a source with no
    punctuation at all — common in ASR output — still gets chopped, by word,
    rather than being handed over whole.
    """
    pieces = [p.strip() for p in _BOUNDARY.split(text) if p and p.strip()]
    out: list[str] = []
    for piece in pieces:
        if len(piece) <= max_chars:
            out.append(piece)
            continue
        words, cur = piece.split(), ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > max_chars:
                out.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            out.append(cur)
    return out or ([text.strip()] if text.strip() else [])


async def _translate_fragment(core, fragment: str, *, temperature: float) -> str:
    out = await core.chat(
        [
            ChatMessage(role="system", content=ENGLISH_DICTATION_SYS),
            ChatMessage(role="user", content=f"Fragment:\n{fragment}\n\nEnglish:"),
        ],
        temperature=temperature,
        max_tokens=400,
    )
    return (out.content or "").strip()


async def to_english_clinical(text: str, *, registry) -> str:
    """Translate mixed-language ASR text to English, or return it unchanged.

    Returns the source text verbatim when translation cannot be verified — the
    caller gets something true rather than something readable.
    """
    result, _ = await translate_verified(text, registry=registry)
    return result


async def translate_verified(text: str, *, registry) -> tuple[str, dict]:
    """Translate and verify. Returns ``(text, report)``.

    ``report`` carries what happened, so a caller (or an audit trail) can tell a
    verified translation from a degraded fallback instead of having to trust the
    string. Keys: ``ok``, ``degraded``, ``reason``, ``fragments``, ``attempts``,
    ``source_numerals``, ``output_numerals``, ``missing``, ``invented``.
    """
    raw = (text or "").strip()
    report: dict = {"ok": True, "degraded": False, "reason": "", "fragments": 0,
                    "attempts": 0, "source_numerals": [], "output_numerals": [],
                    "missing": [], "invented": []}
    if not raw:
        return "", report
    if not _has_persian(raw):
        report["reason"] = "already english; returned unchanged"
        return raw, report

    verify = _as_bool(os.environ.get("ASR_TRANSLATE_VERIFY"), True)
    max_chars = int(os.environ.get("ASR_TRANSLATE_SEGMENT_CHARS", "180"))
    max_attempts = max(1, int(os.environ.get("ASR_TRANSLATE_ATTEMPTS", "2")))

    core = await registry.get_text("core")
    fragments = segment(raw, max_chars)
    src_nums = numerals(raw)
    report["fragments"] = len(fragments)
    report["source_numerals"] = src_nums

    best: str | None = None
    best_score = None
    for attempt in range(1, max_attempts + 1):
        report["attempts"] = attempt
        # Retry deterministically: if a sampled pass invented something, more
        # sampling is not the fix.
        temperature = 0.1 if attempt == 1 else 0.0
        parts = []
        for frag in fragments:
            try:
                parts.append(await _translate_fragment(core, frag, temperature=temperature))
            except Exception as e:  # noqa: BLE001
                log.warning("fragment translation failed (%s); keeping source", e)
                parts.append(frag)
        english = " ".join(p for p in parts if p).strip()
        out_nums = numerals(english)

        missing = [n for n in src_nums if out_nums.count(n) < src_nums.count(n)]
        invented = [n for n in out_nums if n not in src_nums]
        score = len(missing) + len(invented)
        if best_score is None or score < best_score:
            best, best_score = english, score
            report["output_numerals"] = out_nums
            report["missing"] = missing
            report["invented"] = invented

        if not verify or score == 0:
            report["ok"] = True
            log.info(
                "translated dictation: %d chars -> %d chars, %d fragment(s), "
                "numerals %s verified",
                len(raw), len(english), len(fragments), src_nums or "(none)",
            )
            return english, report

        log.warning(
            "translation attempt %d failed numeral verification "
            "(missing=%s invented=%s); retrying at temperature 0",
            attempt, missing, invented,
        )

    # Every attempt altered the numbers. Returning the fluent-but-wrong text
    # here is what made this defect dangerous, so return the source instead and
    # say why.
    report["ok"] = False
    report["degraded"] = True
    report["reason"] = (
        f"numeral verification failed after {report['attempts']} attempt(s): "
        f"missing={report['missing']} invented={report['invented']}"
    )
    log.error(
        "translation REJECTED (%s); returning source text unmodified", report["reason"]
    )
    return raw, report
