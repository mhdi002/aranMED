"""Query understanding: rewrite, multi-query, HyDE."""
from __future__ import annotations

import json
import re

from medrag.config import HYDE, MULTI_QUERY, QUERY_REWRITE
from medrag.rag import routing


def _parse_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
    return {}


def rewrite_queries(query: str, language: str = "en") -> dict:
    """Return search variants for multi-query retrieval."""
    out = {"original": query, "queries": [query], "hyde": None}
    if not QUERY_REWRITE and not MULTI_QUERY and not HYDE:
        return out

    lang_note = "Persian/Farsi" if language == "fa" else "English"
    prompt = (
        "You are a medical search query optimizer for a bilingual textbook RAG system.\n"
        f"Question language: {lang_note}\n"
        "Return JSON with:\n"
        '- "queries": 2-4 diverse search queries covering synonyms, medical terms, '
        "and rephrasings\n"
    )
    if language == "fa":
        prompt += (
            "Include at least one English medical query (terminology suitable for "
            "English textbooks) AND keep Persian variants for Iranian standards.\n"
        )
    else:
        prompt += "Prefer the same language as the question.\n"
    if HYDE:
        prompt += (
            '- "hyde": one short hypothetical textbook paragraph (3-5 sentences) '
            "that would answer the question — use medical terminology\n"
        )
    prompt += f"\nQuestion: {query}"

    try:
        raw = routing.ollama_chat(
            [{"role": "user", "content": prompt}], fmt="json", temperature=0.1,
        )
        data = _parse_json(raw)
        queries = [q.strip() for q in data.get("queries", []) if q and q.strip()]
        if MULTI_QUERY and queries:
            merged = [query]
            for q in queries:
                if q.lower() != query.lower() and q not in merged:
                    merged.append(q)
            out["queries"] = merged[:4]
        elif QUERY_REWRITE and queries:
            out["queries"] = [queries[0], query] if queries[0].lower() != query.lower() else [query]
        if HYDE and data.get("hyde"):
            out["hyde"] = str(data["hyde"]).strip()
    except Exception:
        pass
    return out


def decompose_if_complex(query: str) -> list[str]:
    """Split multi-part clinical questions into sub-queries when needed."""
    if len(query) < 120 or query.count("?") <= 1:
        return [query]
    prompt = (
        "If this medical question has independent sub-questions, split them. "
        'Return JSON {"parts": ["...", "..."]} or {"parts": []} if single question.\n\n'
        f"Question: {query}"
    )
    try:
        raw = routing.ollama_chat([{"role": "user", "content": prompt}], fmt="json", temperature=0)
        parts = _parse_json(raw).get("parts", [])
        parts = [p.strip() for p in parts if p and len(p.strip()) > 10]
        return parts if len(parts) >= 2 else [query]
    except Exception:
        return [query]
