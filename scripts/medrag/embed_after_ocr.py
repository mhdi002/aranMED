"""After OCR releases GPU/Qdrant: embed standards / Mehrsys / library / exam.

Usage:
  python scripts/embed_after_ocr.py --standards-only
  python scripts/embed_after_ocr.py --source mehrsys --force
  python scripts/embed_after_ocr.py --source library --force
  python scripts/embed_after_ocr.py --corpus exam --force
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from medrag.index._compat import patch_bge_m3_sparse, patch_torch_load_check

patch_torch_load_check()
patch_bge_m3_sparse()

from medrag.config import QDRANT_MODE, QDRANT_STORAGE, QDRANT_STANDARDS_STORAGE
from medrag.index.build_index import run_embed
from medrag.knowledge.extract_kg import main as seed_kg_main


def qdrant_busy() -> bool:
    """Local-path lock only matters in local mode (server never opens those folders)."""
    if QDRANT_MODE == "server":
        return False
    lock = QDRANT_STORAGE / ".lock"
    return lock.exists()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--standards-only", action="store_true")
    ap.add_argument(
        "--source",
        choices=["standards", "mehrsys", "library", "exam", "all"],
        default=None,
        help="Restrict to one source corpus (overrides --standards-only)",
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--corpus", choices=["library", "all", "exam"], default="library")
    ap.add_argument("--force", action="store_true", help="embed even if .lock present")
    ap.add_argument("--seed-kg", action="store_true", default=True)
    args = ap.parse_args()

    if qdrant_busy() and not args.force:
        print(f"Qdrant locked ({QDRANT_STORAGE / '.lock'}). Wait or pass --force.")
        sys.exit(2)
    # Stale local locks are harmless in server mode but clear them anyway
    std_lock = QDRANT_STANDARDS_STORAGE / ".lock"
    if std_lock.exists():
        std_lock.unlink(missing_ok=True)
    main_lock = QDRANT_STORAGE / ".lock"
    if QDRANT_MODE == "server" and main_lock.exists():
        main_lock.unlink(missing_ok=True)

    if args.seed_kg:
        seed_kg_main()

    source = args.source
    if args.standards_only and not source:
        source = "standards"

    if source == "exam":
        corpus, filt = "exam", None
    elif source == "all":
        corpus, filt = "all", None
    elif source in ("standards", "mehrsys", "library"):
        corpus, filt = "library", source
    else:
        corpus, filt = args.corpus, None

    print(
        f"[embed_after_ocr] mode={QDRANT_MODE} source={source} "
        f"corpus={corpus} filter={filt}",
        flush=True,
    )
    stats = run_embed(
        corpus=corpus,
        cleanup=False,
        source_corpus_filter=filt,
        max_books=args.limit or None,
    )
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
