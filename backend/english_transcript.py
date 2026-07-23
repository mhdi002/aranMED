"""Translate bilingual ASR output into English clinical dictation."""
from __future__ import annotations

import logging

from providers.base import ChatMessage

log = logging.getLogger("english_transcript")

ENGLISH_DICTATION_SYS = """You convert raw automatic-speech-recognition output from radiology dictation into clear clinical English.

The speaker mixes Persian (Farsi) and English. Your job:
1. Translate ALL Persian content into accurate clinical English.
2. Keep English phrases; fix obvious ASR misspellings (echogenicity, hypoechoic, splenomegaly, hepatomegaly, pancreas, gallbladder, etc.).
3. Preserve every finding, organ, measurement, and qualifier — do NOT drop content.
4. Do NOT invent findings not present in the source.
5. Output ONE continuous English dictation paragraph only (no headings, bullets, or markdown).
"""


async def to_english_clinical(text: str, *, registry) -> str:
    """Return an English-only radiology dictation from mixed-language ASR text."""
    raw = (text or "").strip()
    if not raw:
        return ""
    # Already mostly English with no Persian script — light cleanup only.
    if not _has_persian(raw):
        return raw

    core = await registry.get_text("core")
    out = await core.chat(
        [
            ChatMessage(role="system", content=ENGLISH_DICTATION_SYS),
            ChatMessage(
                role="user",
                content=f"Raw ASR transcript:\n{raw}\n\nEnglish clinical dictation:",
            ),
        ],
        temperature=0.1,
        max_tokens=1400,
    )
    english = out.content.strip()
    log.info("Translated dictation to English (%d -> %d chars)", len(raw), len(english))
    return english


def _has_persian(text: str) -> bool:
    return any("\u0600" <= ch <= "\u06FF" for ch in text)
