"""Contextual chunk headers and parent windows for better retrieval."""
from __future__ import annotations


def contextual_header(title: str, specialty: str, doc_type: str,
                      page_start: int, page_end: int, language: str) -> str:
    """Rule-based contextual prefix (Anthropic-style) — no LLM at index time."""
    pages = f"p.{page_start}" if page_start == page_end else f"pp.{page_start}-{page_end}"
    return (
        f"Document: {title} | Specialty: {specialty} | Type: {doc_type} | "
        f"{pages} | Language: {language}\n"
    )


def embed_text(raw: str, meta: dict, use_header: bool = True) -> str:
    if not use_header:
        return raw
    hdr = contextual_header(
        meta.get("title") or meta.get("book", "?"),
        meta.get("specialty", "general"),
        meta.get("doc_type", "textbook"),
        meta.get("page_start") or meta.get("page", 0),
        meta.get("page_end") or meta.get("page", 0),
        meta.get("language", "en"),
    )
    return hdr + raw


def attach_parent_text(chunks: list[dict], page_texts: dict[int, str] | None = None) -> list[dict]:
    """Parent = full page text when available, else chunk text (small→large expand)."""
    for c in chunks:
        page = c.get("page") or c.get("page_start")
        parent = None
        if page_texts and page in page_texts:
            parent = page_texts[page]
        elif c.get("page_start") != c.get("page_end"):
            parent = c["text"]
        c["parent_text"] = parent or c["text"]
    return chunks


def build_page_map_from_pages(pages: list[dict]) -> dict[int, str]:
    return {p["page"]: p["text"] for p in pages}


def gen_display_text(hit: dict, use_parent: bool = True) -> str:
    """Text shown to LLM — prefer parent window when child was retrieved."""
    if use_parent and hit.get("parent_text") and len(hit["parent_text"]) > len(hit.get("text", "")):
        return hit["parent_text"]
    return hit.get("text", "")
