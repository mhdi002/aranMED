"""Overnight pipeline: OCR → chunk → embed → full eval (sequential, resumable)."""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

LOG = ROOT / "reports" / "overnight_pipeline.log"


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_stage(name: str, *args: str) -> int:
    log(f"===== START {name} =====")
    cmd = [sys.executable, str(ROOT / "run_pipeline.py"), *args]
    env = {
        **dict(__import__("os").environ),
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.run(cmd, cwd=ROOT, env=env)
    log(f"===== END {name} (exit {proc.returncode}) =====")
    return proc.returncode


def run_eval(n: int = 20, skip_ocr: bool = False) -> int:
    log("===== START full_eval =====")
    cmd = [sys.executable, "-m", "medrag.eval.run_full_suite", "--all", "--n", str(n)]
    if skip_ocr:
        cmd.append("--skip-ocr")
    env = {
        **dict(__import__("os").environ),
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.run(cmd, cwd=ROOT, env=env)
    log(f"===== END full_eval (exit {proc.returncode}) =====")
    return proc.returncode


def main():
    ap = argparse.ArgumentParser(description="OCR → chunk → embed → eval")
    ap.add_argument("--skip-ocr", action="store_true", help="skip OCR stage")
    ap.add_argument("--skip-eval", action="store_true")
    ap.add_argument("--eval-n", type=int, default=20)
    ap.add_argument("--ocr-only", action="store_true")
    ap.add_argument("--post-ocr-only", action="store_true", help="chunk+embed+eval only")
    args = ap.parse_args()

    log("overnight pipeline started")

    if not args.post_ocr_only and not args.skip_ocr:
        rc = run_stage("ocr", "--only", "ocr")
        if rc != 0:
            log(f"OCR exited {rc} — continuing with partial OCR data")

    if args.ocr_only:
        log("ocr-only mode — done")
        return

    run_stage("chunk_embed_exam", "--only", "chunk", "embed", "--corpus", "exam")
    run_stage("embed_library", "--only", "embed", "--corpus", "library")

    if not args.skip_eval:
        # OCR eval needs samples; retrieval/RAG verify use Qdrant
        run_eval(n=args.eval_n, skip_ocr=False)

    # Final status
    try:
        from medrag.index import vectorstore as vs
        import sqlite3
        from medrag.config import CATALOG_DB
        conn = sqlite3.connect(CATALOG_DB)
        lib = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(n_chunks),0) FROM documents WHERE source_corpus='library'"
        ).fetchone()
        exam = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(n_chunks),0) FROM documents WHERE source_corpus='exam'"
        ).fetchone()
        conn.close()
        log(f"FINAL qdrant={vs.count()} library_books={lib[0]} exam_books={exam[0]}")
    except Exception as e:
        log(f"FINAL status error: {e}")

    log("overnight pipeline complete")


if __name__ == "__main__":
    main()
