"""Move inactive root-level qdrant_*_data archives under data/qdrant_archives/.

SAFE: does NOT move qdrant_storage (live server path).
Does NOT delete anything. Refuses if a .lock exists or destination already exists.

  python -u scripts/relocate_qdrant_archives.py
  python -u scripts/relocate_qdrant_archives.py --dry-run
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHIVES = (
    "qdrant_data",
    "qdrant_expand_data",
    "qdrant_expand2_data",
    "qdrant_expand3_data",
    "qdrant_standards_data",
)
# Never relocate — live Qdrant server storage while embed/server runs
LIVE_FORBIDDEN = "qdrant_storage"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    dest_root = ROOT / "data" / "qdrant_archives"
    print(f"ROOT={ROOT}")
    print(f"DEST={dest_root}")
    print(f"(never moves {LIVE_FORBIDDEN})")

    if not args.dry_run:
        dest_root.mkdir(parents=True, exist_ok=True)

    moved = 0
    for name in ARCHIVES:
        src = ROOT / name
        dst = dest_root / name
        if not src.is_dir():
            print(f"  skip {name}: not at root")
            continue
        if (src / ".lock").exists():
            print(f"  SKIP {name}: .lock present — not safe")
            continue
        if dst.exists():
            print(f"  SKIP {name}: destination already exists: {dst}")
            continue
        print(f"  MOVE {src.name} -> {dst}")
        if not args.dry_run:
            shutil.move(str(src), str(dst))
        moved += 1

    live = ROOT / LIVE_FORBIDDEN
    print(f"  leave {LIVE_FORBIDDEN}: {'present (LIVE)' if live.is_dir() else 'missing'}")
    print(f"Done. moved={moved} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
