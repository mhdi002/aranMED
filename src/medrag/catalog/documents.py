"""Document lifecycle: content hashing, re-index detection, stale vector cleanup.

Every indexed book is tracked by a stable book_id and a content_hash of its
source material. When OCR pages are added, a PDF is replaced, or text is
re-extracted, the hash changes and the next embed pass replaces (not duplicates)
vectors in Qdrant.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pathlib import Path

from medrag.config import CATALOG_DB, MANIFEST, OCR_OUT, TEXT_OUT


def _backfill_book_ids(conn: sqlite3.Connection):
    """Ensure every row has a stable book_id derived from path/title."""
    rows = conn.execute(
        "SELECT rowid, book_id, title, specialty, file_path, source_corpus, doc_hash "
        "FROM documents WHERE book_id IS NULL OR book_id = ''"
    ).fetchall()
    for rowid, book_id, title, specialty, file_path, corpus, doc_hash in rows:
        fp = file_path or ""
        stem = Path(fp).stem if fp else (title or "unknown")
        spec = specialty or "general"
        if corpus == "exam":
            bid = f"exam:{stem}"
        elif corpus == "standards":
            bid = f"std:{spec}:{stem}"
        elif corpus == "mehrsys":
            bid = f"mehrsys:{spec}:{stem}"
        else:
            bid = f"lib:{spec}:{stem}"
        try:
            conn.execute("UPDATE documents SET book_id=? WHERE rowid=?", (bid, rowid))
        except sqlite3.IntegrityError:
            # Duplicate book_id already claimed — keep a unique suffix
            conn.execute(
                "UPDATE documents SET book_id=? WHERE rowid=?",
                (f"{bid}#{rowid}", rowid),
            )
    conn.commit()


def _md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def hash_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def hash_pages_jsonl(stem: str) -> str | None:
    """Hash extracted/OCR page JSONL — changes when OCR adds pages or text is re-run."""
    h = hashlib.md5()
    found = False
    for d in (OCR_OUT, TEXT_OUT):
        p = d / f"{stem}.jsonl"
        if not p.exists():
            continue
        found = True
        h.update(p.name.encode())
        with open(p, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
    return h.hexdigest() if found else None


def exam_content_hash(stem: str, pdf_path: str | None) -> str | None:
    """Content fingerprint for exam corpus: PDF bytes + page JSONL (OCR or text)."""
    h = hashlib.md5()
    got = False
    if pdf_path and Path(pdf_path).exists():
        with open(pdf_path, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        got = True
    page_hash = hash_pages_jsonl(stem)
    if page_hash:
        h.update(page_hash.encode())
        got = True
    return h.hexdigest() if got else None


def init_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            book_id TEXT PRIMARY KEY,
            content_hash TEXT,
            indexed_hash TEXT,
            doc_hash TEXT,
            title TEXT,
            specialty TEXT,
            file_path TEXT,
            language TEXT,
            doc_type TEXT,
            source_corpus TEXT,
            n_chunks INTEGER DEFAULT 0,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS embed_progress (
            book_id TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            doc_hash TEXT NOT NULL,
            chunks_total INTEGER NOT NULL,
            chunks_done INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        );
    """)
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(documents)")}
    # Migrate legacy medrag catalog (doc_hash was PRIMARY KEY, no book_id)
    if "book_id" not in cols and "doc_hash" in cols:
        conn.execute("ALTER TABLE documents RENAME TO documents_legacy")
        conn.executescript("""
            CREATE TABLE documents (
                book_id TEXT PRIMARY KEY,
                content_hash TEXT,
                indexed_hash TEXT,
                doc_hash TEXT,
                title TEXT,
                specialty TEXT,
                file_path TEXT,
                language TEXT,
                doc_type TEXT,
                source_corpus TEXT,
                n_chunks INTEGER DEFAULT 0,
                updated_at TEXT
            );
        """)
        for row in conn.execute("SELECT * FROM documents_legacy"):
            legacy = dict(zip(
                [c[1] for c in conn.execute("PRAGMA table_info(documents_legacy)")],
                row,
            ))
            fp = legacy.get("file_path") or ""
            stem = Path(fp).stem if fp else legacy.get("title", "unknown")
            spec = legacy.get("specialty") or "general"
            corpus = legacy.get("source_corpus") or "library"
            if corpus == "exam":
                book_id = f"exam:{stem}"
            else:
                book_id = f"lib:{spec}:{stem}"
            conn.execute(
                "INSERT OR IGNORE INTO documents "
                "(book_id, content_hash, indexed_hash, doc_hash, title, specialty, "
                "file_path, language, doc_type, source_corpus, n_chunks, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    book_id,
                    legacy.get("content_hash") or legacy.get("doc_hash"),
                    legacy.get("indexed_hash") or legacy.get("doc_hash"),
                    legacy.get("doc_hash"),
                    legacy.get("title"),
                    legacy.get("specialty"),
                    fp,
                    legacy.get("language"),
                    legacy.get("doc_type"),
                    corpus,
                    legacy.get("n_chunks") or 0,
                    legacy.get("updated_at") or legacy.get("added_at"),
                ),
            )
        conn.execute("DROP TABLE documents_legacy")
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(documents)")}

    migrations = [
        ("content_hash", "TEXT"), ("indexed_hash", "TEXT"), ("doc_hash", "TEXT"),
        ("updated_at", "TEXT"), ("book_id", "TEXT"), ("language", "TEXT"),
        ("doc_type", "TEXT"), ("source_corpus", "TEXT"),
        ("edition", "TEXT"), ("year", "INTEGER"), ("supersedes", "TEXT"),
        ("country", "TEXT"), ("active", "INTEGER"),
    ]
    for col, typ in migrations:
        if col not in cols:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {typ}")
    conn.commit()
    _backfill_book_ids(conn)
    try:
        from medrag.knowledge.schema import init_knowledge_schema
        init_knowledge_schema(conn)
    except Exception:
        pass


def get_record(conn: sqlite3.Connection, book_id: str) -> dict | None:
    row = conn.execute(
        "SELECT book_id, content_hash, indexed_hash, doc_hash, n_chunks, source_corpus "
        "FROM documents WHERE book_id=?",
        (book_id,),
    ).fetchone()
    if not row:
        return None
    keys = ("book_id", "content_hash", "indexed_hash", "doc_hash", "n_chunks", "source_corpus")
    return dict(zip(keys, row))


def store_for_corpus(source_corpus: str | None = None, book_id: str | None = None) -> str:
    """Map corpus / book_id → Qdrant store name."""
    from medrag.config import EMBED_WRITE_STORE
    if source_corpus == "standards" or (book_id and str(book_id).startswith("std:")):
        return "standards"
    if source_corpus in ("mehrsys", "library", "exam") or (
        book_id and (
            str(book_id).startswith("mehrsys:")
            or str(book_id).startswith("lib:")
            or str(book_id).startswith("exam:")
        )
    ):
        return EMBED_WRITE_STORE  # server: medical_library_expand
    return "main"


def is_fully_indexed(conn: sqlite3.Connection, book_id: str, content_hash: str) -> bool:
    """True when book is completely embedded and vectors exist in Qdrant."""
    return not needs_reindex(conn, book_id, content_hash)


def _qdrant_chunk_count(doc_hash: str, store: str = "main") -> int:
    try:
        from medrag.config import QDRANT_MODE
        from medrag.index import vectorstore as vs
        from qdrant_client import models
        stores = [store]
        # In server mode only query the write store(s) — never open local main.
        # expand2/3 may exist as empty server collections; skip missing quietly.
        if store in ("expand", "expand2", "expand3"):
            if QDRANT_MODE == "server":
                stores = ["expand", "expand2", "expand3"]
            else:
                stores = ["expand", "expand2", "expand3", "main"]
        total = 0
        for s in stores:
            try:
                name = vs.collection_name(s)
                c = vs.client(s)
                if not c.collection_exists(name):
                    continue
                result = c.count(
                    name,
                    count_filter=models.Filter(must=[
                        models.FieldCondition(
                            key="doc_hash", match=models.MatchValue(value=doc_hash)
                        )
                    ]),
                    exact=True,
                )
                total += result.count
            except Exception:
                continue
        return total
    except Exception:
        return 0


def get_embed_resume(conn: sqlite3.Connection, book_id: str,
                     content_hash: str, doc_hash: str,
                     source_corpus: str | None = None) -> int:
    """Return chunk offset to resume from (0 = start fresh)."""
    row = conn.execute(
        "SELECT content_hash, doc_hash, chunks_done FROM embed_progress WHERE book_id=?",
        (book_id,),
    ).fetchone()
    if not row:
        return 0
    prog_hash, prog_doc, done = row
    if prog_hash != content_hash or prog_doc != doc_hash:
        clear_embed_progress(conn, book_id)
        return 0
    store = store_for_corpus(source_corpus, book_id)
    in_qdrant = _qdrant_chunk_count(doc_hash, store=store)
    if in_qdrant < done:
        clear_embed_progress(conn, book_id)
        return 0
    return done


def save_embed_progress(conn: sqlite3.Connection, book_id: str, content_hash: str,
                        doc_hash: str, chunks_total: int, chunks_done: int):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO embed_progress "
        "(book_id, content_hash, doc_hash, chunks_total, chunks_done, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (book_id, content_hash, doc_hash, chunks_total, chunks_done, now),
    )
    conn.commit()


def clear_embed_progress(conn: sqlite3.Connection, book_id: str):
    conn.execute("DELETE FROM embed_progress WHERE book_id=?", (book_id,))
    conn.commit()


def needs_reindex(conn: sqlite3.Connection, book_id: str, content_hash: str) -> bool:
    """True unless catalog + Qdrant show a complete index for this content hash.

    Partial books (vectors < n_chunks) are treated as needing reindex.
    Set MEDRAG_VERIFY_QDRANT=0 to trust catalog hash/n_chunks only (faster skips).
    """
    import os

    rec = get_record(conn, book_id)
    if not rec or not rec.get("indexed_hash"):
        return True
    if rec["content_hash"] != content_hash or rec["indexed_hash"] != content_hash:
        return True
    expected = int(rec.get("n_chunks") or 0)
    if expected <= 0:
        return True

    verify = os.environ.get("MEDRAG_VERIFY_QDRANT", "1") != "0"
    if not verify:
        return False

    doc_hash = rec.get("doc_hash")
    if not doc_hash:
        return True
    store = store_for_corpus(rec.get("source_corpus"), book_id)
    try:
        count = _qdrant_chunk_count(doc_hash, store=store)
        # Incomplete upserts must be re-queued (not "any vectors ⇒ done")
        return count < expected
    except Exception:
        # If Qdrant is locked/unavailable, trust catalog rather than re-embed everything
        return False


def mark_indexed(conn, book_id: str, content_hash: str, doc_hash: str,
                 title: str, specialty: str, file_path: str, language: str,
                 doc_type: str, source_corpus: str, n_chunks: int):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO documents "
        "(book_id, content_hash, indexed_hash, doc_hash, title, specialty, file_path, "
        "language, doc_type, source_corpus, n_chunks, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (book_id, content_hash, content_hash, doc_hash, title, specialty, file_path,
         language, doc_type, source_corpus, n_chunks, now),
    )
    conn.commit()
    clear_embed_progress(conn, book_id)


def purge_stale_vectors(conn: sqlite3.Connection) -> int:
    """Stale-vector cleanup (safe no-op).

    Older logic compared ``doc_hash != indexed_hash`` and deleted matches. That was
    always true because ``doc_hash`` is ``{source}:{content_hash[:24]}`` while
    ``indexed_hash`` is the full content hash — so every live book was purged.

    Edition replacement already deletes the previous ``doc_hash`` inside
    ``upsert_chunks`` when content changes. Do not revive bulk purge without a
    dedicated ``previous_doc_hash`` column.
    """
    return 0


def remove_book(conn: sqlite3.Connection, book_id: str) -> bool:
    """Drop a book from catalog and Qdrant (for explicit removal or edition replace)."""
    rec = get_record(conn, book_id)
    if not rec:
        return False
    if rec.get("doc_hash"):
        from medrag.index import vectorstore as vs
        store = store_for_corpus(rec.get("source_corpus"), book_id)
        vs.delete_by_doc(rec["doc_hash"], store=store)
    conn.execute("DELETE FROM documents WHERE book_id=?", (book_id,))
    clear_embed_progress(conn, book_id)
    conn.commit()
    return True
