"""Unpack Iranian standards archives and register text PDFs for embedding."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from medrag.config import STANDARDS_DIR, STANDARDS_EXTRACTED
from medrag.ingest.standards import scan_standards, unpack_all


def main():
    ap = argparse.ArgumentParser(description="Ingest standards/rules archives")
    ap.add_argument("--limit", type=int, default=0, help="max archives to unpack (0=all)")
    ap.add_argument("--skip-unpack", action="store_true")
    ap.add_argument("--embed", action="store_true",
                    help="embed standards into Qdrant (use when OCR is idle)")
    ap.add_argument("--embed-limit", type=int, default=0,
                    help="embed at most N standards PDFs (0=all pending)")
    args = ap.parse_args()

    if not args.skip_unpack:
        if not STANDARDS_DIR or not Path(STANDARDS_DIR).exists():
            print(f"standards_dir missing: {STANDARDS_DIR}")
            sys.exit(1)
        print(f"Unpacking from {STANDARDS_DIR} -> {STANDARDS_EXTRACTED}")
        stats = unpack_all(limit=args.limit)
        print("Unpack:", json.dumps(stats, indent=2))

    entries = scan_standards()
    by_type: dict[str, int] = {}
    for e in entries:
        by_type[e["doc_type"]] = by_type.get(e["doc_type"], 0) + 1
    print(f"Registered {len(entries)} text-layer PDFs: {by_type}")

    if args.embed:
        from medrag.index.build_index import run_embed
        # Prefer embedding only standards via library scan filter path
        stats = run_embed(corpus="library", cleanup=False, book=None,
                          source_corpus_filter="standards",
                          max_books=args.embed_limit or None)
        print("Embed:", json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
