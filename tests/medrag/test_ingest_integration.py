"""End-to-end two-column PDF extraction test."""
import fitz
import pytest

from medrag.ingest.extract_text import extract_pdf


def make_two_column_pdf(path):
    doc = fitz.open()
    page = doc.new_page(width=500, height=400)
    page.insert_text((180, 30), "ARTICLE TITLE")
    page.insert_text((20, 80), "Left column first sentence about diagnosis.")
    page.insert_text((20, 200), "Left column second sentence about treatment.")
    page.insert_text((270, 90), "Right column first sentence about prognosis.")
    page.insert_text((270, 210), "Right column second sentence about follow-up.")
    page.insert_text((20, 370), "Page 1 of 1")
    doc.save(path)
    doc.close()


@pytest.mark.skipif(not hasattr(fitz, "open"), reason="PyMuPDF not available")
def test_extract_pdf_preserves_column_reading_order(tmp_path):
    pdf_path = tmp_path / "two_col.pdf"
    make_two_column_pdf(str(pdf_path))
    pages = list(extract_pdf(pdf_path))
    assert len(pages) == 1
    _, text = pages[0]
    left_first = text.find("Left column first")
    left_second = text.find("Left column second")
    right_first = text.find("Right column first")
    right_second = text.find("Right column second")
    assert -1 not in (left_first, left_second, right_first, right_second)
    assert left_first < left_second < right_first < right_second
