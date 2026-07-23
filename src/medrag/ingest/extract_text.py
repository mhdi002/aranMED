"""Layout-aware text extraction for PDFs and EPUBs — skip unchanged books."""
from __future__ import annotations

import json
import re
from pathlib import Path

from medrag.catalog.documents import hash_file
from medrag.config import TEXT_OUT
from medrag.ingest import layout
from medrag.ingest.pdf_io import open_pdf, page_text, summarize_mupdf_noise, suppress_mupdf_errors


def extract_pdf(path: Path):
    with suppress_mupdf_errors() as mupdf_buf:
        doc = open_pdf(path)
        try:
            n_pages = len(doc)
            page_width = doc[0].rect.width if n_pages else 0
            raw_pages = []
            for pageno in range(n_pages):
                page = doc[pageno]
                raw_pages.append(page_text(page, page_width))
            cleaned = layout.strip_repeated_boilerplate(raw_pages)
            for pageno, text in enumerate(cleaned, 1):
                if text and text.strip():
                    yield pageno, text
        finally:
            doc.close()

    note = summarize_mupdf_noise(mupdf_buf)
    if note:
        print(f"  [warn] {path.name[:50]:50s} {note}", flush=True)


def extract_epub(path: Path):
    try:
        from ebooklib import epub, ITEM_DOCUMENT
    except ImportError as e:
        raise ImportError(
            "EPUB extraction requires ebooklib — run: pip install ebooklib>=0.18"
        ) from e
    book = epub.read_epub(str(path))
    for i, item in enumerate(book.get_items_of_type(ITEM_DOCUMENT)):
        html = item.get_content().decode("utf-8", "ignore")
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        if text.strip():
            yield i + 1, text


def extract(path: Path):
    from medrag.ingest.mehrsys import extract_pages, is_mehrsys_pack

    p = Path(path)
    if is_mehrsys_pack(p):
        yield from extract_pages(p)
        return
    ext = p.suffix.lower()
    if ext == ".pdf":
        yield from extract_pdf(p)
    elif ext == ".epub":
        yield from extract_epub(p)


def _sidecar_hash(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".hash")


def extract_book(meta: dict, out_dir: Path | None = None, force: bool = False) -> int:
    """Extract one book to JSONL pages. Skips if PDF unchanged unless force=True."""
    out_dir = out_dir or TEXT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(meta["file"]).stem
    out = out_dir / f"{stem}.jsonl"
    pdf = Path(meta["path"])
    pdf_hash = hash_file(pdf) if pdf.exists() else ""
    sidecar = _sidecar_hash(out)

    if not force and out.exists() and sidecar.exists():
        if sidecar.read_text(encoding="utf-8").strip() == pdf_hash:
            return sum(1 for _ in open(out, encoding="utf-8"))

    pages = list(extract(pdf))
    with open(out, "w", encoding="utf-8") as f:
        for pageno, text in pages:
            f.write(json.dumps({
                "book": meta["file"],
                "language": meta.get("language", "en"),
                "specialty": meta.get("specialty", "general"),
                "type": meta.get("type", "textbook"),
                "page": pageno,
                "text": text,
            }, ensure_ascii=False) + "\n")
    if pdf_hash:
        sidecar.write_text(pdf_hash, encoding="utf-8")
    return len(pages)


def extract_all(manifest_rows: list[dict], force: bool = False) -> dict:
    stats = {"ok": 0, "skip": 0, "pages": 0, "unchanged": 0}
    for rec in manifest_rows:
        if rec.get("duplicate") or rec.get("needs_ocr") or rec.get("excluded"):
            stats["skip"] += 1
            continue
        stem = Path(rec["file"]).stem
        out = TEXT_OUT / f"{stem}.jsonl"
        sidecar = _sidecar_hash(out)
        pdf = Path(rec["path"])
        pdf_hash = hash_file(pdf) if pdf.exists() else ""
        if not force and out.exists() and sidecar.exists() and sidecar.read_text().strip() == pdf_hash:
            stats["unchanged"] += 1
            continue
        n = extract_book(rec, force=force)
        stats["ok"] += 1
        stats["pages"] += n
        print(f"  {rec['file'][:50]:50s} {n:4d} pages")
    return stats
