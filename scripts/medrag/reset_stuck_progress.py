"""Reset stuck embed_progress so incomplete books restart on server expand collection."""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
db = ROOT / "catalog.db"


def main():
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "select book_id, chunks_done, chunks_total from embed_progress"
    ).fetchall()
    print("before:", rows)
    # Clear stuck/incomplete progress only — re-embed from chunk 0 into server.
    # Do NOT delete completed documents (catalog stays intact).
    conn.execute("DELETE FROM embed_progress")
    conn.commit()
    print("cleared embed_progress")
    ms = conn.execute(
        "select count(1), coalesce(sum(n_chunks),0) from documents where source_corpus='mehrsys'"
    ).fetchone()
    print("mehrsys kept in catalog:", ms)
    conn.close()
    print("OK — next: ensure Qdrant server, then python -u scripts/embed_all_live.py")
    print("Writes go to medical_library_expand on http://localhost:6333")


if __name__ == "__main__":
    main()
