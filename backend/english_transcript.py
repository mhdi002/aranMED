"""Translate bilingual ASR output into English clinical dictation.

This module is the second stage of `/api/transcribe`: Whisper produces Persian,
and this turns it into English. It is also where a clinical-safety defect lived,
so the design is deliberately defensive.

Given the whole transcript as one blob, a small model asked to "translate
radiology dictation" does not translate -- it *writes a radiology report*. On a
real 41.9 s sample it invented the anatomy (a portal vein the source never
mentions), invented negative findings ("no thrombus", "no focal lesions"),
invented a measurement, and inverted a negation. Three runs of identical input
at temperature 0.1 gave three mutually incompatible clinical readings, each
fluent enough to pass as a better transcript than the truth.

Three mechanisms address that, because prompting alone demonstrably did not:
the previous prompt already said "Preserve every finding, organ, measurement,
and qualifier" and "Do NOT invent findings not present in the source", and the
model disregarded both.

1. SEGMENTED translation. Short fragments are translated independently, so the
   model never holds enough context to compose a plausible whole-study
   narrative. It can only render what is in front of it.
2. NUMERAL verification. Every number in the source must appear in the output,
   and any number in the output with no basis in the source is a fabrication.
   Measurements are what a clinician cannot re-derive from context, which makes
   them both the highest-stakes content and a cheap, objective check.
3. TARGETED REPAIR. A failed check is fed back to the model naming the exact
   numbers at issue, which corrects far more reliably than a blind retry --
   the model is usually mis-*reading* one number, not ignoring the task.

Numbers appear in the source as digits ("50") *and* as words ("سی" = thirty),
and a faithful translation renders both as digits. Comparing digits alone
therefore flags correct translations as fabrications, so word forms are
resolved on both sides before comparing. Observed: source `سی در 51`
(thirty by 51), model output `31 x 51` -- a genuinely wrong measurement that
must be caught, which is only distinguishable from a correct `30 x 51` once
`سی` is known to mean 30.

The output stays ENGLISH even when verification ultimately fails. Returning the
Persian source protects fidelity but breaks every downstream consumer -- report
generation, template selection, the rules engine -- so the failure is reported
via `translation_degraded` rather than by handing back text the pipeline cannot
read. Set ASR_TRANSLATE_FALLBACK=source for the opposite trade.

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
3. Copy EVERY number exactly. Never round, merge, drop, or add one. "50 در 50 در 50" is "50 x 50 x 50". Persian number WORDS become digits with their exact value: "سی" is 30 (NOT 31), "بیست" is 20, "پنجاه" is 50, "دو" is 2.
4. Preserve negation. "نه" / "no" / "بدون" means the finding is ABSENT — never render an absent finding as present, or a present one as absent. Note "نه" here means "no", it is not the number nine.
5. Add NOTHING. Do not add findings, organs, measurements, normal statements, or reassurances. Never write phrases like "no thrombus was identified" or "no focal lesions are seen" unless the fragment itself says so. Inventing a negative finding is as dangerous as inventing a positive one.
6. If a fragment is garbled or you cannot tell what it means, transliterate it as literally as you can. Do NOT guess at a plausible clinical meaning.
7. Output ONLY the translated fragment. No headings, no markdown, no commentary, no added sentences.
"""

REPAIR_SYS = """You are correcting NUMBERS in a translation. Change nothing else.

You get a Persian/English source fragment and its English translation. The numbers disagree with the source.

RULES:
1. Compare every number in the translation against the source and fix it.
2. Persian number words map to their exact value: "سی"=30, "بیست"=20, "چهل"=40, "پنجاه"=50, "شصت"=60, "هفتاد"=70, "هشتاد"=80, "نود"=90, "صد"=100, "دو"=2, "سه"=3.
3. "نه" before a finding means "no"/"not" — it is NOT the number nine.
4. Remove any number that has no counterpart in the source. Add any source number that is missing.
5. Keep all other wording exactly as it is. Output ONLY the corrected English fragment.
"""

# Persian/Arabic-Indic digits, so a number written in either script compares.
_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# Number WORDS a faithful translation renders as digits. Without these, a
# correct rendering of "سی" as "30" looks like a fabricated number.
#
# "نه" (nine) is deliberately ABSENT: it is far more commonly the negation
# "no" in clinical dictation ("نه با سکولاریتی" = "no vascularity"), and
# admitting it here would let a genuinely invented 9 pass unnoticed. Ambiguous
# words are safer excluded — the cost is a false rejection, which the repair
# pass then resolves, rather than a fabrication served as fact.
_NUM_WORDS: dict[str, int] = {
    # Persian
    "یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5,
    "شش": 6, "هفت": 7, "هشت": 8, "ده": 10,
    "یازده": 11, "دوازده": 12, "سیزده": 13, "چهارده": 14, "پانزده": 15,
    "شانزده": 16, "هفده": 17, "هجده": 18, "نوزده": 19,
    "بیست": 20, "سی": 30, "چهل": 40, "پنجاه": 50,
    "شصت": 60, "هفتاد": 70, "هشتاد": 80, "نود": 90, "صد": 100,
    # English, for the code-switched half of the dictation
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}

# Split on sentence/clause boundaries in either script.
_BOUNDARY = re.compile(r"(?<=[.!?؟۔؛،;،])\s+|\n+")


def _as_bool(value: str | None, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _has_persian(text: str) -> bool:
    return any("؀" <= ch <= "ۿ" for ch in text)


def numerals(text: str) -> list[str]:
    """Digits in `text`, with Persian/Arabic-Indic forms normalised."""
    return re.findall(r"\d+", (text or "").translate(_DIGIT_MAP))


def word_numerals(text: str) -> list[str]:
    """Numbers written as words, as their digit strings."""
    if not text:
        return []
    found: list[str] = []
    for token in re.findall(r"[^\W\d_]+", text, flags=re.UNICODE):
        value = _NUM_WORDS.get(token.lower())
        if value is not None:
            found.append(str(value))
    return found


def source_numerals(text: str) -> list[str]:
    """Every number the source states, written as digits OR as words.

    This is the set a faithful translation may legitimately contain.
    """
    return numerals(text) + word_numerals(text)


def segment(text: str, max_chars: int) -> list[str]:
    """Split into fragments of at most ~max_chars, preferring clause breaks.

    Fragments are what bound the model's freedom to invent, so a source with no
    punctuation at all -- common in ASR output -- still gets chopped, by word,
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


# Clinical opposites that a translation must not flip. A number the model
# invents is caught by the numeral check; a polarity it inverts is not, and
# reads as a completely normal finding. Observed on real dictation: source
# "هایپوکوک" (HYPOechoic) rendered as "HYPERechoic" -- one syllable, opposite
# meaning, and nothing downstream can tell.
_POLARITY_PAIRS: tuple[tuple[str, str], ...] = (
    ("hypoechoic", "hyperechoic"),
    ("hypodense", "hyperdense"),
    ("hypointense", "hyperintense"),
    ("hypoplastic", "hyperplastic"),
    ("hypotrophy", "hypertrophy"),
)


def polarity_conflicts(source: str, english: str) -> list[str]:
    """Terms the translation states as the opposite of the source.

    Only fires when the source clearly carries one pole and the output carries
    only the other, so a fragment mentioning both is left alone rather than
    guessed at.
    """
    src = (source or "").lower()
    out = (english or "").lower()
    # The Persian transliterations these arrive as, so the source side is
    # detectable before translation has happened.
    src_lo_hint = "هایپو" in src or "hypo" in src
    src_hi_hint = "هایپر" in src or "hyper" in src
    issues: list[str] = []
    for lo, hi in _POLARITY_PAIRS:
        if (src_lo_hint and not src_hi_hint) and hi in out and lo not in out:
            issues.append(f"source says hypo-, translation says {hi}")
            break
        if (src_hi_hint and not src_lo_hint) and lo in out and hi not in out:
            issues.append(f"source says hyper-, translation says {lo}")
            break
    return issues


def compare(source: str, english: str) -> tuple[list[str], list[str]]:
    """Return ``(missing, invented)`` numbers for one source/translation pair."""
    src = source_numerals(source)
    out = numerals(english) + word_numerals(english)
    missing = [n for n in numerals(source) if out.count(n) < numerals(source).count(n)]
    invented = [n for n in out if n not in src]
    return missing, invented


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


async def _repair_fragment(core, fragment: str, english: str,
                           missing: list[str], invented: list[str]) -> str:
    """Re-ask for just the numbers, naming exactly what disagrees.

    A blind retry re-runs the same misreading; naming the discrepancy gives the
    model the one piece of information it lacked.
    """
    problems = []
    if invented:
        problems.append(
            f"these numbers appear in the translation but NOT in the source: "
            f"{', '.join(invented)}"
        )
    if missing:
        problems.append(
            f"these numbers appear in the source but NOT in the translation: "
            f"{', '.join(missing)}"
        )
    out = await core.chat(
        [
            ChatMessage(role="system", content=REPAIR_SYS),
            ChatMessage(
                role="user",
                content=(
                    f"Source fragment:\n{fragment}\n\n"
                    f"Current translation:\n{english}\n\n"
                    f"Problem: {'; and '.join(problems)}.\n\n"
                    f"Corrected English:"
                ),
            ),
        ],
        temperature=0.0,
        max_tokens=400,
    )
    return (out.content or "").strip()


async def to_english_clinical(text: str, *, registry) -> str:
    """Translate mixed-language ASR text to English."""
    result, _ = await translate_verified(text, registry=registry)
    return result


async def translate_verified(text: str, *, registry) -> tuple[str, dict]:
    """Translate and verify. Returns ``(text, report)``.

    ``report`` carries what happened, so a caller (or an audit trail) can tell a
    verified translation from an unverified one instead of having to trust the
    string. Keys: ``ok``, ``degraded``, ``reason``, ``fragments``, ``repaired``,
    ``source_numerals``, ``output_numerals``, ``missing``, ``invented``.
    """
    raw = (text or "").strip()
    report: dict = {"ok": True, "degraded": False, "reason": "", "fragments": 0,
                    "repaired": 0, "untranslated_fragments": 0,
                    "source_numerals": [], "output_numerals": [],
                    "missing": [], "invented": []}
    if not raw:
        return "", report
    if not _has_persian(raw):
        report["reason"] = "already english; returned unchanged"
        return raw, report

    verify = _as_bool(os.environ.get("ASR_TRANSLATE_VERIFY"), True)
    max_chars = int(os.environ.get("ASR_TRANSLATE_SEGMENT_CHARS", "180"))
    repairs = max(0, int(os.environ.get("ASR_TRANSLATE_REPAIRS", "2")))
    fallback = (os.environ.get("ASR_TRANSLATE_FALLBACK") or "english").strip().lower()

    core = await registry.get_text("core")
    fragments = segment(raw, max_chars)
    report["fragments"] = len(fragments)
    report["source_numerals"] = source_numerals(raw)

    parts: list[str] = []
    all_missing: list[str] = []
    all_invented: list[str] = []
    all_polarity: list[str] = []
    untranslated = 0

    for frag in fragments:
        try:
            english = await _translate_fragment(core, frag, temperature=0.1)
        except Exception as e:  # noqa: BLE001
            # Keeping the source keeps the numbers, but the fragment is now
            # Persian in an otherwise-English transcript. Count it: reporting
            # ok=True here would tell the caller "verified English" about text
            # that was never translated -- which is how a provider outage
            # looks identical to a clean run.
            log.warning("fragment translation failed (%s); keeping source", e)
            parts.append(frag)
            untranslated += 1
            continue

        if verify:
            missing, invented = compare(frag, english)
            attempt = 0
            while (missing or invented) and attempt < repairs:
                attempt += 1
                try:
                    english = await _repair_fragment(
                        core, frag, english, missing, invented
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("numeral repair failed (%s)", e)
                    break
                missing, invented = compare(frag, english)
                if not missing and not invented:
                    report["repaired"] += 1
                    log.info("numeral repair succeeded on attempt %d", attempt)
            all_missing.extend(missing)
            all_invented.extend(invented)
            all_polarity.extend(polarity_conflicts(frag, english))

        parts.append(english)

    result = " ".join(p for p in parts if p).strip()
    report["output_numerals"] = numerals(result)
    report["missing"] = all_missing
    report["invented"] = all_invented

    if untranslated:
        report["ok"] = False
        report["degraded"] = True
        report["untranslated_fragments"] = untranslated
        report["reason"] = (
            f"{untranslated} of {len(fragments)} fragment(s) could not be "
            f"translated (provider unreachable?); those remain in the source "
            f"language"
        )
        log.error("ASR translation INCOMPLETE: %s", report["reason"])
        return result, report

    report["polarity"] = all_polarity

    if not verify or (not all_missing and not all_invented and not all_polarity):
        log.info(
            "translated dictation: %d -> %d chars, %d fragment(s), %d repaired, "
            "numerals %s verified",
            len(raw), len(result), len(fragments), report["repaired"],
            report["source_numerals"] or "(none)",
        )
        return result, report

    report["ok"] = False
    report["degraded"] = True
    report["reason"] = (
        f"verification failed after {repairs} repair attempt(s): "
        f"missing={all_missing} invented={all_invented} polarity={all_polarity}"
    )
    log.error("ASR translation UNVERIFIED: %s", report["reason"])

    if fallback == "source":
        # Fidelity over usability: hand back the Persian. Every downstream
        # consumer (report generation, template selection, rules) then gets
        # text it cannot read, so this is opt-in only.
        return raw, report
    # Default: keep English so the pipeline keeps working, and rely on
    # `translation_degraded` to tell the caller it is unverified.
    return result, report
