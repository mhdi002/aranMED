"""Unified book registry merging exam manifest + EN library catalog."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from medrag.catalog.manifest import build_exam_manifest, write_manifest
from medrag.config import CATALOG_DB, LIBRARY_DIR, MANIFEST, MEHRSYS_BOOKS_DIR, STANDARDS_EXTRACTED
from medrag.ingest.mehrsys import scan_mehrsys_dir
from medrag.ingest.standards import scan_standards


def _init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            doc_hash TEXT PRIMARY KEY,
            title TEXT,
            specialty TEXT,
            file_path TEXT,
            language TEXT,
            doc_type TEXT,
            source_corpus TEXT,
            n_chunks INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS titles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            specialty TEXT,
            file_path TEXT,
            status TEXT,
            indexed INTEGER DEFAULT 0
        );
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
    for col, typ in [("language", "TEXT"), ("doc_type", "TEXT"), ("source_corpus", "TEXT")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {typ}")
    conn.commit()


def scan_library() -> list[dict]:
    """Scan EN library PDFs/EPUBs + Mehrsys packs + Iranian standards PDFs."""
    entries = []
    if LIBRARY_DIR.exists():
        for specialty_dir in sorted(LIBRARY_DIR.iterdir()):
            if not specialty_dir.is_dir():
                continue
            specialty = specialty_dir.name.replace("_", " ")
            for fp in sorted(specialty_dir.iterdir()):
                if fp.suffix.lower() not in (".pdf", ".epub"):
                    continue
                entries.append({
                    "title": fp.stem,
                    "specialty": specialty,
                    "file_path": str(fp.resolve()),
                    "language": "en",
                    "doc_type": "guideline" if "guideline" in fp.name.lower() else "textbook",
                    "source_corpus": "library",
                    "status": "downloaded",
                    "country": None,
                    "index_tags": ["textbook"],
                })
    entries.extend(scan_mehrsys_dir(MEHRSYS_BOOKS_DIR))
    if STANDARDS_EXTRACTED.exists():
        entries.extend(scan_standards(STANDARDS_EXTRACTED))
    return entries


def sync_registry():
    """Build manifest + sync library files into catalog.db."""
    exam_rows = build_exam_manifest()
    write_manifest(exam_rows)

    conn = sqlite3.connect(CATALOG_DB)
    _init_db(conn)

    for r in exam_rows:
        if r.get("duplicate"):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO documents(doc_hash, title, specialty, file_path, "
            "language, doc_type, source_corpus) VALUES (?,?,?,?,?,?,?)",
            (r["file"], r["file"], r["specialty"], r["path"],
             r["language"], r["type"], "exam"),
        )

    lib_entries = scan_library()
    for e in lib_entries:
        conn.execute(
            "INSERT OR IGNORE INTO titles(title, specialty, file_path, status) VALUES (?,?,?,?)",
            (e["title"], e["specialty"], e["file_path"], e["status"]),
        )
    conn.commit()
    conn.close()

    return {
        "exam_books": len([r for r in exam_rows if not r.get("duplicate")]),
        "library_books": len(lib_entries),
        "manifest": str(MANIFEST),
    }


def load_manifest() -> list[dict]:
    if not MANIFEST.exists():
        return []
    return [json.loads(l) for l in open(MANIFEST, encoding="utf-8")]


def all_specialties() -> list[str]:
    specs = set()
    for r in load_manifest():
        specs.add(r["specialty"])
    if CATALOG_DB.exists():
        conn = sqlite3.connect(CATALOG_DB)
        for row in conn.execute("SELECT DISTINCT specialty FROM titles"):
            specs.add(row[0])
        conn.close()
    return sorted(specs)


def main():
    stats = sync_registry()
    print("Registry synced:", json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
