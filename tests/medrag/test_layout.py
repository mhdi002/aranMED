"""Multi-column PDF reading-order tests."""
from medrag.ingest.layout import (
    blocks_to_text, classify_columns, reading_order, strip_repeated_boilerplate,
)

PAGE_WIDTH = 500


def block(x0, y0, x1, y1, text):
    return (x0, y0, x1, y1, text, 0, 0)


def test_two_column_reading_order():
    header = block(0, 0, 500, 40, "TITLE")
    left1 = block(0, 50, 250, 70, "L1")
    left2 = block(0, 150, 250, 170, "L2")
    right1 = block(260, 60, 500, 80, "R1")
    right2 = block(260, 160, 500, 180, "R2")
    footer = block(0, 300, 500, 320, "Page 1")
    blocks = [right1, footer, left2, header, right2, left1]
    ordered = reading_order(blocks, PAGE_WIDTH)
    assert blocks_to_text(ordered) == "TITLE\nL1\nL2\nR1\nR2\nPage 1"


def test_single_column_falls_back_to_top_to_bottom():
    b1 = block(0, 10, 500, 30, "First")
    b2 = block(0, 40, 500, 60, "Second")
    b3 = block(0, 70, 500, 90, "Third")
    ordered = reading_order([b3, b1, b2], PAGE_WIDTH)
    assert blocks_to_text(ordered) == "First\nSecond\nThird"


def test_classify_columns_straddling_block_is_full_width():
    straddle = block(200, 100, 320, 120, "spans midline")
    full, left, right = classify_columns([straddle], PAGE_WIDTH)
    assert straddle in full
    assert left == [] and right == []


def test_strip_repeated_boilerplate_removes_running_header():
    pages = [
        "Running Header\nContent about drug dosing on page 1\nPage 1",
        "Running Header\nContent about surgery on page 2\nPage 2",
        "Running Header\nContent about diagnosis on page 3\nPage 3",
        "Running Header\nContent about follow-up on page 4\nPage 4",
    ]
    cleaned = strip_repeated_boilerplate(pages, min_pages=4, ratio_threshold=0.5)
    for p in cleaned:
        assert "Running Header" not in p
    assert "drug dosing" in cleaned[0]
