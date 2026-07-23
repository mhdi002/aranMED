"""Embed all pending corpora with live terminal + log (resumable).

Order: Mehrsys → EN library → exam → standards

Continues to the next stage even if a stage exits non-zero (records failures).
Requires Qdrant server when config vector_db.mode=server (default).
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "reports" / "embed_all.log"
LOG_ROTATE_BYTES = 15 * 1024 * 1024

# Logical store → human label for logs
_STAGE_WRITE = {
    "mehrsys": ("expand", "medical_library_expand"),
    "library": ("expand", "medical_library_expand"),
    "exam": ("expand", "medical_library_expand"),
    "standards": ("standards", "medical_library_standards"),
}


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _rotate_log_if_huge():
    if not LOG.exists() or LOG.stat().st_size <= LOG_ROTATE_BYTES:
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    arch = LOG.with_name(f"embed_all_{ts}.log")
    LOG.rename(arch)
    print(f"Archived large log -> {arch.name} ({arch.stat().st_size/1e6:.1f} MB)", flush=True)


def _clear_locks():
    sys.path.insert(0, str(ROOT / "src"))
    from medrag.config import (
        QDRANT_EXPAND2_STORAGE, QDRANT_EXPAND3_STORAGE, QDRANT_EXPAND_STORAGE,
        QDRANT_STANDARDS_STORAGE, QDRANT_STORAGE,
    )
    for store in (
        QDRANT_STORAGE, QDRANT_STANDARDS_STORAGE,
        QDRANT_EXPAND_STORAGE, QDRANT_EXPAND2_STORAGE, QDRANT_EXPAND3_STORAGE,
    ):
        lock = Path(store) / ".lock"
        if lock.exists():
            try:
                lock.unlink(missing_ok=True)
                print(f"Removed stale lock: {lock}", flush=True)
            except OSError as e:
                print(f"[warn] could not remove {lock}: {e}", flush=True)


def _require_qdrant():
    sys.path.insert(0, str(ROOT / "src"))
    from medrag.config import EMBED_WRITE_STORE, QDRANT_MODE, QDRANT_URL
    from medrag.index import vectorstore as vs

    print(f"Qdrant mode={QDRANT_MODE} url={QDRANT_URL} write_store={EMBED_WRITE_STORE}", flush=True)
    if QDRANT_MODE == "server":
        vs.require_server()
        # Pre-create both write targets used by later stages (never open local main)
        for store in (EMBED_WRITE_STORE, "standards"):
            vs.ensure_collection(store)
            print(
                f"Server collection ready: {vs.collection_name(store)}",
                flush=True,
            )
    else:
        print(
            "[warn] vector_db.mode=local will OOM as stores grow. "
            "Set mode: server and start Qdrant.",
            flush=True,
        )


def _ensure_server_for_stage(label: str):
    """Re-check Qdrant before each stage so a mid-run server death fails loudly."""
    sys.path.insert(0, str(ROOT / "src"))
    from medrag.config import QDRANT_MODE
    from medrag.index import vectorstore as vs

    if QDRANT_MODE != "server":
        return
    store, coll = _STAGE_WRITE.get(label, ("expand", "?"))
    vs.require_server()
    vs.ensure_collection(store)
    print(f"[{label}] target store={store} collection={coll}", flush=True)


def _run(label: str, extra: list[str]) -> int:
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        # Trust catalog skips so later stages don't reopen huge indexes to verify
        "MEDRAG_VERIFY_QDRANT": os.environ.get("MEDRAG_VERIFY_QDRANT", "0"),
    }
    cmd = [
        sys.executable, "-u", str(ROOT / "scripts" / "embed_after_ocr.py"),
        "--force", *extra,
    ]
    print(f"\n=== {label} {datetime.now(timezone.utc).isoformat()} ===", flush=True)
    print(" ".join(cmd), flush=True)
    try:
        _ensure_server_for_stage(label)
    except RuntimeError as e:
        print(f"[{label}] FATAL server check: {e}", flush=True)
        with open(LOG, "a", encoding="utf-8") as logf:
            logf.write(f"\n=== {label} start {datetime.now(timezone.utc).isoformat()} ===\n")
            logf.write(f"FATAL: {e}\n=== {label} exit 2 ===\n")
        return 2

    with open(LOG, "a", encoding="utf-8") as logf:
        logf.write(f"\n=== {label} start {datetime.now(timezone.utc).isoformat()} ===\n")
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
        logf.write(f"=== {label} exit {rc} ===\n")
    print(f"=== {label} done (exit {rc}) ===", flush=True)
    if rc != 0:
        print(
            f"[{label}] non-zero exit — continuing to next stage "
            "(will not abort the pipeline)",
            flush=True,
        )
    return rc


def main():
    _utf8()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log_if_huge()
    _clear_locks()
    try:
        _require_qdrant()
    except RuntimeError as e:
        print(f"\nFATAL: {e}", flush=True)
        return 2
    print(f"Log: {LOG}", flush=True)
    print(
        "Pipeline: mehrsys → library → exam → standards "
        "(auto-continues after each stage; skips catalog-done books)",
        flush=True,
    )
    stages = [
        ("mehrsys", ["--source", "mehrsys"]),
        ("library", ["--source", "library"]),
        ("exam", ["--source", "exam"]),
        ("standards", ["--source", "standards"]),
    ]
    failed = []
    for label, args in stages:
        rc = _run(label, args)
        if rc != 0:
            failed.append((label, rc))
    print("\n=== ALL STAGES FINISHED ===", flush=True)
    if failed:
        print(f"Failed stages: {failed}", flush=True)
        return 1
    print("All stages OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
