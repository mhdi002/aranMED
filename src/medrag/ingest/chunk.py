"""Dual chunking: qbank per-question + textbook paragraph windows."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from medrag.config import CFG, CHUNKS, CONTEXTUAL_HEADERS, MANIFEST, OCR_OUT, TEXT_OUT
from medrag.ingest.contextual import attach_parent_text, embed_text

TARGET_CHARS = CFG["chunking"]["qbank_target_chars"]
OVERLAP_CHARS = CFG["chunking"]["qbank_overlap_chars"]
CHUNK_TOKENS = CFG["chunking"]["textbook_tokens"]
CHUNK_OVERLAP = CFG["chunking"]["textbook_overlap"]

Q_START = re.compile(r"(?m)^\s*(?:سوال|سؤال|پرسش)?\s*[\-\.]?\s*(?:[۰-۹0-9]{1,3})\s*[\-\.\)ـ]\s")
FIGURE = re.compile(r"\[FIGURE:([^\]]*)\]", re.IGNORECASE)


def load_pages(book_stem: str):
    for d in (OCR_OUT, TEXT_OUT):
        p = d / f"{book_stem}.jsonl"
        if p.exists():
            rows = [json.loads(l) for l in open(p, encoding="utf-8")]
            rows.sort(key=lambda r: r["page"])
            return rows
    return []


def chunk_text_paragraph(text: str, approx_tokens=CHUNK_TOKENS, overlap=CHUNK_OVERLAP):
    max_chars = approx_tokens * 4
    ov_chars = overlap * 4
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return
    parts = re.split(r"(?<=[.!?])\s+|\n{2,}", text)
    buf = ""
    for p in parts:
        if len(buf) + len(p) + 1 <= max_chars:
            buf += " " + p
        else:
            if buf.strip():
                yield buf.strip()
            buf = (buf[-ov_chars:] + " " + p) if buf else p
    if buf.strip():
        yield buf.strip()


def split_textbook(pages):
    chunks = []
    buf, span_start, cur_page = "", None, None
    for pg in pages:
        if span_start is None:
            span_start = pg["page"]
        cur_page = pg["page"]
        buf += ("\n\n" if buf else "") + pg["text"]
        while len(buf) >= TARGET_CHARS:
            cut = buf.rfind("\n", 0, TARGET_CHARS)
            if cut < TARGET_CHARS * 0.5:
                cut = TARGET_CHARS
            chunks.append((span_start, cur_page, buf[:cut].strip()))
            buf = buf[max(0, cut - OVERLAP_CHARS):].strip()
            span_start = cur_page
    if buf.strip():
        chunks.append((span_start, cur_page, buf.strip()))
    return chunks


def split_qbank(pages):
    chunks = []
    for pg in pages:
        text = pg["text"]
        starts = [m.start() for m in Q_START.finditer(text)]
        if len(starts) < 2:
            chunks.append((pg["page"], pg["page"], text.strip()))
            continue
        starts.append(len(text))
        if starts[0] > 0:
            head = text[:starts[0]].strip()
            if len(head) > 40:
                chunks.append((pg["page"], pg["page"], head))
        for a, b in zip(starts, starts[1:]):
            seg = text[a:b].strip()
            if len(seg) > 15:
                chunks.append((pg["page"], pg["page"], seg))
    return chunks


def chunk_manifest(manifest_rows: list[dict]) -> list[dict]:
    all_chunks = []
    for rec in manifest_rows:
        if rec.get("duplicate") or rec.get("excluded"):
            continue
        stem = os.path.splitext(rec["file"])[0]
        pages = load_pages(stem)
        if not pages:
            continue
        if rec["type"] in ("qbank", "exam"):
            spans = split_qbank(pages)
        else:
            spans = split_textbook(pages)
        for i, (ps, pe, text) in enumerate(spans):
            if not text:
                continue
            figs = FIGURE.findall(text)
            all_chunks.append({
                "id": f"{stem}::{ps}-{pe}::{i}",
                "book": rec["file"],
                "title": rec["file"],
                "language": rec["language"],
                "specialty": rec["specialty"],
                "doc_type": rec["type"],
                "source_corpus": "exam",
                "page_start": ps,
                "page_end": pe,
                "page": ps,
                "text": text,
                "has_image": bool(figs),
                "figure_notes": figs,
            })
        print(f"  {rec['language']} {stem[:44]:44s} {len(pages):4d}p -> {len(spans):4d} chunks",
              flush=True)
    return all_chunks


def chunk_library_file(path: Path, title: str, specialty: str,
                       *, language: str = "en", doc_type: str = "textbook",
                       source_corpus: str = "library", country: str | None = None,
                       index_tags: list[str] | None = None,
                       year: int | None = None, version: str | None = None,
                       use_sections: bool | None = None) -> list[dict]:
    """Chunk a library / Mehrsys / standards file into embeddable passages."""
    from medrag.ingest.extract_text import extract
    from medrag.ingest.mehrsys import is_mehrsys_pack, pack_dir
    from medrag.ingest.section_parse import section_chunks_from_pages

    path = Path(path)
    is_ms = is_mehrsys_pack(path)
    book_name = pack_dir(path).name if is_ms else path.name
    if is_ms:
        source_corpus = "mehrsys"
        language = language or "en"
        doc_type = doc_type or "textbook"

    if use_sections is None:
        use_sections = source_corpus == "standards" or doc_type in (
            "standard", "legal", "guideline", "textbook",
        )

    page_texts: dict[int, str] = {}
    pages_list: list[tuple[int, str]] = []
    for pageno, page_text in extract(path):
        page_texts[pageno] = page_text
        pages_list.append((pageno, page_text))

    chunks = []
    if use_sections and source_corpus == "standards":
        sec_chunks = section_chunks_from_pages(
            pages_list, corpus=source_corpus, specialty=specialty, title=title,
        )
        for sc in sec_chunks:
            if len(sc["text"]) < CFG["chunking"]["min_chunk_chars"]:
                continue
            meta = {
                "book": book_name,
                "title": title,
                "language": language,
                "specialty": specialty,
                "doc_type": doc_type,
                "source_corpus": source_corpus,
                "page": sc["page"],
                "page_start": sc["page_start"],
                "page_end": sc["page_end"],
                "chunk_id": sc["chunk_id"],
                "cko_id": sc["cko_id"],
                "section_title": sc.get("section_title"),
                "topic": sc.get("topic"),
                "country": country or ("IR" if source_corpus == "standards" else None),
                "year": year,
                "version": version,
                "index_tags": index_tags or [doc_type],
                "active": True,
                "evidence_level": None,
                "recommendation_class": None,
                "population": None,
            }
            chunks.append({
                "id": sc["chunk_id"],
                **meta,
                "text": sc["text"],
                "embed_text": embed_text(sc["text"], meta, CONTEXTUAL_HEADERS),
            })
    else:
        # Optional section headers for textbooks; fall back to paragraph windows
        for pageno, page_text in pages_list:
            parts = list(chunk_text_paragraph(page_text))
            for i, ch in enumerate(parts):
                if len(ch) < CFG["chunking"]["min_chunk_chars"]:
                    continue
                meta = {
                    "book": book_name,
                    "title": title,
                    "language": language,
                    "specialty": specialty,
                    "doc_type": doc_type,
                    "source_corpus": source_corpus,
                    "page": pageno,
                    "page_start": pageno,
                    "page_end": pageno,
                    "country": country,
                    "year": year,
                    "version": version,
                    "index_tags": index_tags or (
                        ["textbook"] if doc_type == "textbook" else [doc_type]
                    ),
                    "active": True,
                }
                cid = f"{Path(book_name).stem}::p{pageno}::{i}"
                chunks.append({
                    "id": cid,
                    "chunk_id": cid,
                    **meta,
                    "text": ch,
                    "embed_text": embed_text(ch, meta, CONTEXTUAL_HEADERS),
                })
    return attach_parent_text(chunks, page_texts)


def write_chunks(chunks: list[dict], out: Path | None = None):
    out = out or CHUNKS
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"Wrote {len(chunks)} chunks -> {out}")
    return len(chunks)


def main():
    rows = [json.loads(l) for l in open(MANIFEST, encoding="utf-8")]
    chunks = chunk_manifest(rows)
    write_chunks(chunks)


if __name__ == "__main__":
    main()
