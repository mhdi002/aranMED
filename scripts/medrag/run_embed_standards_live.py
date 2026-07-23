"""Run standards embed with live terminal output + log file."""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "reports" / "embed_standards.log"


def _force_utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    _force_utf8_stdio()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / "src"))
    from medrag.config import QDRANT_STANDARDS_STORAGE, QDRANT_STORAGE
    for store in (QDRANT_STORAGE, QDRANT_STANDARDS_STORAGE):
        lock = Path(store) / ".lock"
        if lock.exists():
            lock.unlink(missing_ok=True)
            print(f"Removed stale Qdrant lock: {lock}", flush=True)

    cmd = [
        sys.executable, "-u", str(ROOT / "scripts" / "embed_after_ocr.py"),
        "--standards-only", "--force",
    ]
    env = {
        **dict(__import__("os").environ),
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
    }
    print(f"=== standards embed started {datetime.now(timezone.utc).isoformat()} ===", flush=True)
    print(" ".join(cmd), flush=True)
    print(f"Log: {LOG}", flush=True)
    print("-" * 60, flush=True)

    with open(LOG, "a", encoding="utf-8") as logf:
        logf.write(f"\n=== start {datetime.now(timezone.utc).isoformat()} ===\n")
        proc = subprocess.Popen(
            cmd, cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                print(line, end="", flush=True)
            except UnicodeEncodeError:
                sys.stdout.buffer.write(line.encode("utf-8", errors="replace"))
                sys.stdout.buffer.flush()
            logf.write(line)
            logf.flush()
        rc = proc.wait()
        logf.write(f"=== exit {rc} ===\n")
    print("-" * 60, flush=True)
    print(f"Done (exit {rc}). Log: {LOG}", flush=True)
    sys.exit(rc)


if __name__ == "__main__":
    main()
