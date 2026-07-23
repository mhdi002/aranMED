"""Heading/section-aware document parse + semantic chunk IDs."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

HEADING_RE = re.compile(
    r"(?m)^\s*(?:"
    r"فصل\s*[\d۰-۹]+|"
    r"ماده\s*[\d۰-۹]+|"
    r"بند\s*[\d۰-۹]+|"
    r"بخش\s*[\d۰-۹]+|"
    r"پیوست\s*[\d۰-۹]*|"
    r"Chapter\s+\d+|"
    r"Section\s+\d+|"
    r"ARTICLE\s+\d+|"
    r"\d+(?:\.\d+){0,3}\s+[A-Z\u0600-\u06FF]"
    r")[^\n]{0,120}$",
    re.I,
)


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^\w\u0600-\u06FF]+", "_", text.strip(), flags=re.U)
    s = re.sub(r"_+", "_", s).strip("_")
    return (s or "sec")[:max_len]


def make_chunk_id(corpus: str, specialty: str, topic: str, seq: int) -> str:
    base = f"{corpus}_{slugify(specialty)}_{slugify(topic)}_{seq:04d}"
    return base[:80]


def make_cko_id(corpus: str, specialty: str, topic: str, seq: int) -> str:
    return f"CKO_{make_chunk_id(corpus, specialty, topic, seq)}"


def split_into_sections(text: str) -> list[tuple[str, str]]:
    """Return list of (heading, body) from a page or multi-page blob."""
    if not text or not text.strip():
        return []
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return [("Body", text.strip())]
    sections: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            sections.append(("Preamble", preamble))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        heading = m.group(0).strip()
        body = block[len(heading):].strip() if block.startswith(heading) else block
        sections.append((heading, f"{heading}\n\n{body}".strip()))
    return sections


def section_chunks_from_pages(
    pages: list[tuple[int, str]],
    *,
    corpus: str,
    specialty: str,
    title: str,
    max_chars: int = 2600,
    min_chars: int = 200,
) -> list[dict]:
    """Build section-bounded chunks with stable IDs from (page_no, text) pairs."""
    # Group consecutive pages into one stream with page markers for attribution
    chunks: list[dict] = []
    seq = 0
    for pageno, page_text in pages:
        for heading, body in split_into_sections(page_text):
            parts = _window(body, max_chars)
            for part in parts:
                if len(part) < min_chars:
                    continue
                seq += 1
                topic = heading or title
                chunk_id = make_chunk_id(corpus, specialty, topic, seq)
                cko_id = make_cko_id(corpus, specialty, topic, seq)
                chunks.append({
                    "chunk_id": chunk_id,
                    "cko_id": cko_id,
                    "section_title": heading,
                    "topic": slugify(heading, 60).replace("_", " "),
                    "page": pageno,
                    "page_start": pageno,
                    "page_end": pageno,
                    "text": part,
                })
    return chunks


def _window(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    out = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            # break at paragraph
            cut = text.rfind("\n\n", start, end)
            if cut > start + max_chars // 2:
                end = cut
        out.append(text[start:end].strip())
        start = end
    return [x for x in out if x]


def content_fingerprint(*parts: str) -> str:
    h = hashlib.md5()
    for p in parts:
        h.update(p.encode("utf-8", errors="ignore"))
    return h.hexdigest()
