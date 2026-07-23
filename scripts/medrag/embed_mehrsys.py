"""Embed only Mehrsys SQLite book packs (resumable)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("MEDRAG_VERIFY_QDRANT", "0")  # trust catalog for fast skip

from medrag.index.build_index import run_embed


if __name__ == "__main__":
    print(run_embed(corpus="library", cleanup=False, source_corpus_filter="mehrsys"))
