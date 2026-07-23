"""Extractive context compression — keep only query-relevant sentences."""
from __future__ import annotations

import re

from medrag.config import COMPRESS_CONTEXT, RETRIEVAL
from medrag.index import embedder


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?؟])\s+|\n+", text)
    return [p.strip() for p in parts if len(p.strip()) > 20]


def compress_passage(text: str, query: str, max_chars: int | None = None) -> str:
    """Rerank sentences within a passage; return most relevant subset."""
    max_chars = max_chars or RETRIEVAL.get("gen_chars_per_chunk", 1600)
    if not COMPRESS_CONTEXT or len(text) <= max_chars:
        return text[:max_chars]

    sents = _sentences(text)
    if len(sents) <= 2:
        return text[:max_chars]

    try:
        scores = embedder.rerank(query, sents)
        ranked = sorted(zip(scores, sents), reverse=True)
    except Exception:
        return text[:max_chars]

    out, total = [], 0
    for _, s in ranked:
        if total + len(s) + 1 > max_chars:
            break
        out.append(s)
        total += len(s) + 1
    if not out:
        return text[:max_chars]
    return " ".join(out)


def compress_passages(passages: list[dict], query: str) -> list[dict]:
    max_chars = RETRIEVAL.get("gen_chars_per_chunk", 1600)
    out = []
    for p in passages:
        text = p.get("display_text") or p.get("text", "")
        compressed = compress_passage(text, query, max_chars)
        out.append({**p, "display_text": compressed})
    return out
