"""Wait for Qdrant lock (OCR) to clear, then embed standards (+ pending library).

Runs in background; logs to reports/embed_after_ocr.log
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from medrag.config import QDRANT_STORAGE, REPORTS_DIR  # noqa: E402

LOG = REPORTS_DIR / "embed_after_ocr.log"
LOCK = Path(QDRANT_STORAGE) / ".lock"


def log(msg: str):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode(), flush=True)


def main():
    log("Waiting for Qdrant lock to clear (OCR)...")
    while LOCK.exists():
        time.sleep(60)
    # Brief settle so OCR process fully exits
    time.sleep(30)
    if LOCK.exists():
        log("Lock reappeared; continuing to wait")
        return main()
    log("Qdrant free — embedding standards (all pending) then library pending")
    env = {**dict(**{k: v for k, v in __import__("os").environ.items()}),
           "PYTHONPATH": str(ROOT / "src"),
           "PYTHONIOENCODING": "utf-8"}
    cmd = [sys.executable, str(ROOT / "scripts" / "embed_after_ocr.py"),
           "--standards-only", "--force"]
    r = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True)
    log(f"standards embed exit={r.returncode}")
    if r.stdout:
        log(r.stdout[-2000:])
    if r.stderr:
        log("STDERR: " + r.stderr[-1000:])
    # Then continue library/mehrsys pending (no filter)
    cmd2 = [sys.executable, "-c",
            "from medrag.index.build_index import run_embed; "
            "import json; print(json.dumps(run_embed('library', cleanup=True), indent=2))"]
    r2 = subprocess.run(cmd2, cwd=str(ROOT), env=env, capture_output=True, text=True)
    log(f"library embed exit={r2.returncode}")
    if r2.stdout:
        log(r2.stdout[-2000:])
    log("Done.")


if __name__ == "__main__":
    main()
