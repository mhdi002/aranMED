"""One-command incremental ingestion orchestrator."""
from __future__ import annotations

import argparse
import os
import sys

# Allow running without pip install -e .
_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_ROOT, "src"))

ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="MedicalRAG ingestion pipeline")
    ap.add_argument("--skip", nargs="*", default=[])
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--corpus", choices=["all", "exam", "library"], default="all")
    ap.add_argument("--book", default=None, help="single book for ocr/embed resume")
    args = ap.parse_args()

    stages = [
        ("registry", _registry),
        ("extract_text", _extract_text),
        ("ocr", _ocr),
        ("chunk", _chunk),
        ("embed", _embed),
    ]

    for name, fn in stages:
        if args.only and name not in args.only:
            continue
        if name in args.skip:
            print(f"== skip {name} ==")
            continue
        print(f"\n===== STAGE: {name} =====", flush=True)
        fn(corpus=args.corpus, book=args.book)


def _registry(**_):
    from medrag.catalog.registry import sync_registry
    print(sync_registry())


def _extract_text(**_):
    from medrag.catalog.registry import load_manifest
    from medrag.ingest.extract_text import extract_all
    rows = load_manifest()
    extract_all(rows)


def _ocr(corpus="all", book=None, **_):
    from medrag.catalog.registry import load_manifest
    from medrag.ingest.ocr_chandra import ocr_all
    ocr_all(load_manifest(), book_name=book)


def _chunk(**_):
    from medrag.catalog.registry import load_manifest
    from medrag.ingest.chunk import chunk_manifest, write_chunks
    write_chunks(chunk_manifest(load_manifest()))


def _embed(corpus="all", book=None, **_):
    from medrag.index.build_index import run_embed
    print(run_embed(corpus=corpus, book=book))


if __name__ == "__main__":
    main()
