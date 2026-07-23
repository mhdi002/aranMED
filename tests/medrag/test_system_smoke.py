"""System smoke checks that do not require live GPU/LLM (structure + imports)."""
from __future__ import annotations


def test_import_pipeline_modules():
    from medrag.index import build_index, embedder, vectorstore  # noqa: F401
    from medrag.rag import engine, retrieval, routing, query, grounding, self_rag  # noqa: F401
    from medrag.rules import engine as rules  # noqa: F401
    from medrag.ingest import mehrsys, standards, chunk  # noqa: F401


def test_config_dual_qdrant_paths():
    from medrag.config import (
        QDRANT_ARCHIVES_DIR, QDRANT_EXPAND_STORAGE, QDRANT_SERVER_STORAGE,
        QDRANT_STANDARDS_STORAGE, QDRANT_STORAGE, SPARSE_EMBED,
    )
    assert "qdrant_data" in QDRANT_STORAGE.name or QDRANT_STORAGE.name.endswith("qdrant_data")
    assert "standards" in QDRANT_STANDARDS_STORAGE.name
    assert "expand" in QDRANT_EXPAND_STORAGE.name
    assert QDRANT_SERVER_STORAGE.name == "qdrant_storage" or "qdrant_storage" in str(QDRANT_SERVER_STORAGE)
    assert "qdrant_archives" in str(QDRANT_ARCHIVES_DIR).replace("\\", "/")
    assert SPARSE_EMBED is False
