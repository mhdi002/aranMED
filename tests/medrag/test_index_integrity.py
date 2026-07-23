"""Tests for index integrity: store routing, partial reindex, sparse query."""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

from medrag.catalog.documents import (
    init_schema, mark_indexed, needs_reindex, store_for_corpus,
)
from medrag.index.vectorstore import rrf_merge_lists, sparse_query_vector
from medrag.rag.routing import _filters_for_intent


def test_store_for_corpus():
    assert store_for_corpus("standards") == "standards"
    assert store_for_corpus(None, "std:genetics:MLPA") == "standards"
    assert store_for_corpus("mehrsys") == "expand"
    assert store_for_corpus("library") == "expand"
    assert store_for_corpus("exam", "exam:foo") == "expand"


def test_needs_reindex_partial_book(tmp_path):
    db = tmp_path / "c.db"
    conn = sqlite3.connect(db)
    init_schema(conn)
    mark_indexed(
        conn, "lib:x:book", "hash1", "lib:abc", "Book", "x", "/b.pdf",
        "en", "textbook", "library", 100,
    )
    with patch("medrag.catalog.documents._qdrant_chunk_count", return_value=40):
        with patch.dict("os.environ", {"MEDRAG_VERIFY_QDRANT": "1"}):
            assert needs_reindex(conn, "lib:x:book", "hash1") is True
    with patch("medrag.catalog.documents._qdrant_chunk_count", return_value=100):
        with patch.dict("os.environ", {"MEDRAG_VERIFY_QDRANT": "1"}):
            assert needs_reindex(conn, "lib:x:book", "hash1") is False


def test_sparse_query_empty_skips():
    assert sparse_query_vector({}) is None
    assert sparse_query_vector({"10": 0.5}) is not None


def test_rrf_merge_dedupes_by_chunk():
    a = [{"doc_hash": "d1", "chunk_id": "c1", "text": "a", "page": 1}]
    b = [{"doc_hash": "d1", "chunk_id": "c1", "text": "a", "page": 1}]
    merged = rrf_merge_lists([a, b])
    assert len(merged) == 1


def test_intent_filters_softened():
    legal = _filters_for_intent("legal")
    assert legal.get("source_corpus") == ["standards"]
    assert "index_tags" not in legal
    clinical = _filters_for_intent("clinical")
    assert "exam" in clinical.get("doc_types", [])


def test_needs_broaden_uses_ce_not_dead_threshold():
    from medrag.rag.retrieval import _needs_broaden
    weak = [{"score": 0.9, "rerank_raw": -2.5}]
    strong = [{"score": 0.3, "rerank_raw": 2.0}]
    assert _needs_broaden([]) is True
    assert _needs_broaden(weak) is True
    assert _needs_broaden(strong) is False


def test_purge_stale_is_safe_noop(tmp_path):
    from medrag.catalog.documents import purge_stale_vectors
    conn = sqlite3.connect(tmp_path / "c.db")
    init_schema(conn)
    mark_indexed(
        conn, "lib:x:b", "hash1", "lib:abcdef", "B", "x", "/b.pdf",
        "en", "textbook", "library", 10,
    )
    assert purge_stale_vectors(conn) == 0
