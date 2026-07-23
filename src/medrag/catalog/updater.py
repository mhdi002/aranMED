"""Add, update, or remove books from the unified index.

Usage:
    python -m medrag.catalog.updater add /path/to/book.pdf --specialty cardiology
    python -m medrag.catalog.updater remove --book-id lib:cardiology:Harrison_21
    python -m medrag.catalog.updater refresh --corpus exam
    python -m medrag.catalog.updater status
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path

from medrag.catalog.documents import init_schema, remove_book
from medrag.catalog.registry import sync_registry
from medrag.config import CATALOG_DB, LIBRARY_DIR
from medrag.index.build_index import run_embed


def add_book(path: Path, specialty: str):
    dest_dir = LIBRARY_DIR / specialty.replace(" ", "_")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if not dest.exists():
        shutil.copy2(path, dest)
    print(f"Added {dest}")
    sync_registry()
    return run_embed(corpus="library")


def cmd_status():
    conn = sqlite3.connect(CATALOG_DB)
    init_schema(conn)
    rows = conn.execute(
        "SELECT source_corpus, COUNT(*), SUM(n_chunks) FROM documents GROUP BY source_corpus"
    ).fetchall()
    for r in rows:
        print(f"  {r[0]}: {r[1]} books, {r[2]} chunks")
    conn.close()
    from medrag.index import vectorstore as vs
    print(f"  Qdrant total: {vs.count()} points")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add")
    p_add.add_argument("path")
    p_add.add_argument("--specialty", required=True)

    p_rm = sub.add_parser("remove")
    p_rm.add_argument("--book-id", required=True)

    sub.add_parser("refresh")
    sub.add_parser("status")

    args = ap.parse_args()
    if args.cmd == "add":
        add_book(Path(args.path), args.specialty)
    elif args.cmd == "remove":
        conn = sqlite3.connect(CATALOG_DB)
        init_schema(conn)
        ok = remove_book(conn, args.book_id)
        print("removed" if ok else "not found")
    elif args.cmd == "refresh":
        sync_registry()
        print(run_embed(corpus="all"))
    elif args.cmd == "status":
        cmd_status()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
