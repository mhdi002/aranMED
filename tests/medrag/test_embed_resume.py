"""Tests for resumable embed progress tracking."""
import sqlite3
from unittest.mock import patch

from medrag.catalog.documents import (
    clear_embed_progress, get_embed_resume, init_schema, save_embed_progress,
)


@patch("medrag.catalog.documents._qdrant_chunk_count", return_value=64)
def test_embed_progress_resume(_mock_count, tmp_path):
    db = tmp_path / "cat.db"
    conn = sqlite3.connect(db)
    init_schema(conn)
    save_embed_progress(conn, "lib:x:book", "hash1", "lib:abc", 100, 64)
    assert get_embed_resume(conn, "lib:x:book", "hash1", "lib:abc") == 64
    assert get_embed_resume(conn, "lib:x:book", "hash2", "lib:abc") == 0
    clear_embed_progress(conn, "lib:x:book")
    assert get_embed_resume(conn, "lib:x:book", "hash1", "lib:abc") == 0
