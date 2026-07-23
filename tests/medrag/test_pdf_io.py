"""Tests for resilient PDF extraction."""
import fitz

from medrag.ingest.pdf_io import open_pdf, page_text, suppress_mupdf_errors


def test_open_and_extract_minimal_pdf(tmp_path):
    p = tmp_path / "t.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hyperkalemia causes peaked T waves.")
    doc.save(str(p))
    doc.close()

    with suppress_mupdf_errors():
        d = open_pdf(p)
        text = page_text(d[0], d[0].rect.width)
        d.close()
    assert "Hyperkalemia" in text


def test_suppress_mupdf_errors_context():
    with suppress_mupdf_errors() as buf:
        pass
    assert buf.getvalue() is not None
