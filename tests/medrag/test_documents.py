"""Tests for document lifecycle hashing and re-index detection."""
import json
import sqlite3

from medrag.catalog.documents import (
    exam_content_hash,
    hash_file,
    init_schema,
    mark_indexed,
    needs_reindex,
)


def test_hash_file_changes_with_content(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello", encoding="utf-8")
    h1 = hash_file(p)
    p.write_text("world", encoding="utf-8")
    h2 = hash_file(p)
    assert h1 != h2


def test_exam_content_hash_changes_with_pages(monkeypatch, tmp_path):
    import medrag.catalog.documents as docmod
    monkeypatch.setattr(docmod, "OCR_OUT", tmp_path)
    monkeypatch.setattr(docmod, "TEXT_OUT", tmp_path / "text")
    (tmp_path / "text").mkdir()
    stem = "book1"
    pdf = tmp_path / "book1.pdf"
    pdf.write_bytes(b"%PDF-1.4 partial")
    j = tmp_path / "text" / f"{stem}.jsonl"
    j.write_text(json.dumps({"page": 1, "text": "a"}) + "\n", encoding="utf-8")
    h1 = exam_content_hash(stem, str(pdf))
    j.write_text(
        json.dumps({"page": 1, "text": "a"}) + "\n"
        + json.dumps({"page": 2, "text": "b"}) + "\n",
        encoding="utf-8",
    )
    h2 = exam_content_hash(stem, str(pdf))
    assert h1 != h2


def test_needs_reindex_on_content_change(tmp_path):
    db = tmp_path / "cat.db"
    conn = sqlite3.connect(db)
    init_schema(conn)
    mark_indexed(
        conn, "exam:x", "hash_v1", "exam:hash_v1", "t", "s", "", "fa", "qbank", "exam", 10,
    )
    assert needs_reindex(conn, "exam:x", "hash_v2")
