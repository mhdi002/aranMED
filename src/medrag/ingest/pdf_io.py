"""Resilient PyMuPDF I/O — suppress MuPDF noise, fallback text extraction."""
from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

from medrag.ingest import layout


@contextlib.contextmanager
def suppress_mupdf_errors():
    """Silence MuPDF stderr spam from broken XObjects/fonts in vendor PDFs."""
    import fitz

    restored = True
    try:
        fitz.TOOLS.mupdf_display_errors(False)
        restored = False
    except Exception:
        pass
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            yield buf
    finally:
        if not restored:
            try:
                fitz.TOOLS.mupdf_display_errors(True)
            except Exception:
                pass


def open_pdf(path: Path):
    """Open PDF from bytes so MuPDF can recover some damaged files."""
    import fitz

    data = path.read_bytes()
    try:
        return fitz.open(stream=data, filetype="pdf")
    except Exception:
        return fitz.open(str(path))


def _layout_page_text(page, page_width: float) -> str:
    blocks = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
    if not blocks:
        return ""
    ordered = layout.reading_order(blocks, page.rect.width or page_width)
    return layout.blocks_to_text(ordered)


def _plain_page_text(page) -> str:
    import fitz

    flags = fitz.TEXT_DEHYPHENATE
    try:
        flags |= fitz.TEXT_PRESERVE_WHITESPACE
    except Exception:
        pass
    text = page.get_text("text", flags=flags)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def page_text(page, page_width: float) -> str:
    """Layout-aware extraction with plain-text fallback per page."""
    for extractor in (
        lambda: _layout_page_text(page, page_width),
        lambda: _plain_page_text(page),
    ):
        try:
            text = extractor()
            if text and len(text.strip()) >= 5:
                return text.strip()
        except Exception:
            continue
    try:
        return _plain_page_text(page)
    except Exception:
        return ""


def summarize_mupdf_noise(buf: io.StringIO) -> str | None:
    noise = buf.getvalue()
    if not noise.strip():
        return None
    if "XObject" in noise or "cannot find" in noise:
        return "broken PDF XObject/font refs — used text fallback"
    if "cmsOpenProfileFromMem" in noise or "format error" in noise:
        return "color profile issues — used text fallback"
    return "MuPDF warnings — extraction continued"
