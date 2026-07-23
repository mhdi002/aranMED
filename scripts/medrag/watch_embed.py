"""Live embed progress watcher — log tail + process + progress DB.

  python -u scripts/watch_embed.py
  python -u scripts/watch_embed.py --loop

Stuck warning only when embed_progress.updated_at is stale >3 min AND
no MedicalRAG embed python process is alive.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys_path_src = str(ROOT / "src")
if sys_path_src not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path_src)
from medrag.config import CATALOG_DB, MEHRSYS_BOOKS_DIR, REPORTS_DIR  # noqa: E402

LOG = REPORTS_DIR / "embed_all.log"
MEHRSYS = Path(MEHRSYS_BOOKS_DIR) if MEHRSYS_BOOKS_DIR else Path()
TAIL_BYTES = 256_000  # last ~256KB only
STALE_PROGRESS_SEC = 180


def ms_target() -> int:
    if not MEHRSYS.exists():
        return 179
    return sum(
        1 for p in MEHRSYS.iterdir()
        if p.is_dir() and (p / "info.json").exists() and (p / "fts.db").exists()
    )


def catalog_counts():
    conn = sqlite3.connect(str(CATALOG_DB))
    out = {}
    for corp in ("mehrsys", "library", "exam", "standards"):
        d, c = conn.execute(
            "select count(1), coalesce(sum(n_chunks),0) from documents where source_corpus=?",
            (corp,),
        ).fetchone()
        out[corp] = (d, c)
    prog = conn.execute(
        "select book_id, chunks_done, chunks_total, updated_at from embed_progress"
    ).fetchall()
    conn.close()
    return out, prog


def find_embed_processes():
    """Return list of {pid, cpu, rss_mb, cmd} for MedicalRAG embed-related python."""
    found = []
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        for p in psutil.process_iter(["pid", "name", "cmdline", "cpu_percent", "memory_info"]):
            try:
                name = (p.info.get("name") or "").lower()
                if "python" not in name:
                    continue
                cmd = " ".join(p.info.get("cmdline") or [])
                if not cmd:
                    continue
                low = cmd.lower()
                if "medicalrag" not in low and "medrag" not in low:
                    continue
                if not any(
                    k in low
                    for k in (
                        "embed_all", "embed_after", "embed_mehrsys",
                        "build_index", "run_pipeline",
                    )
                ):
                    continue
                rss = 0.0
                mi = p.info.get("memory_info")
                if mi is not None:
                    rss = getattr(mi, "rss", 0) / (1024 * 1024)
                found.append({
                    "pid": p.info["pid"],
                    "cpu": p.info.get("cpu_percent") or 0.0,
                    "rss_mb": rss,
                    "cmd": cmd[-80:],
                })
            except (psutil.Error, TypeError, ValueError):
                continue
        return found

    # Fallback without psutil (Windows WMI)
    try:
        import subprocess
        out = subprocess.check_output(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" | "
                "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
            ],
            text=True,
            errors="replace",
            timeout=15,
        )
        import json
        data = json.loads(out) if out.strip() else []
        if isinstance(data, dict):
            data = [data]
        for row in data:
            cmd = row.get("CommandLine") or ""
            low = cmd.lower()
            if "medicalrag" not in low and "medrag" not in low:
                continue
            if not any(k in low for k in ("embed_all", "embed_after", "embed_mehrsys", "build_index")):
                continue
            found.append({
                "pid": row.get("ProcessId"),
                "cpu": None,
                "rss_mb": None,
                "cmd": cmd[-80:],
            })
    except Exception:
        pass
    return found


def parse_updated_age(updated_at: str | None) -> float | None:
    if not updated_at:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            dt = datetime.strptime(updated_at[:26], fmt)
            return time.time() - dt.timestamp()
        except ValueError:
            continue
    return None


def read_log_tail(max_bytes: int = TAIL_BYTES) -> str:
    if not LOG.exists():
        return ""
    size = LOG.stat().st_size
    with open(LOG, "rb") as f:
        if size > max_bytes:
            f.seek(-max_bytes, os.SEEK_END)
        data = f.read()
    return data.decode("utf-8", errors="replace")


def parse_log_tail(text: str):
    starts = list(re.finditer(r"=== (\w+) start ", text))
    stage = starts[-1].group(1) if starts else "?"
    run = text[starts[-1].start():] if starts else text
    oks = re.findall(r"\[ok\]\s+(\w+)\s+.+?->\s+(\d+) chunks", run)
    fails = len(re.findall(r"^\[fail\]", run, re.M))
    by = Counter(c for c, _ in oks)
    cur = None
    for m in re.finditer(r"^\s{4}(.{1,45}?)\s+(\d+)/(\d+) chunks\s*$", run, re.M):
        cur = (m.group(1).strip(), int(m.group(2)), int(m.group(3)))
    resumes = re.findall(r"\[resume\][^\n]*", run)
    exit_m = re.search(r"=== (\w+) exit ([^\n]+)", run)
    stage_exit = exit_m.group(0).strip() if exit_m else None
    last = [f"[ok] {c} -> {n}" for c, n in oks[-6:]]
    return stage, by, cur, last, fails, resumes[-1] if resumes else None, stage_exit


def once():
    cats, prog = catalog_counts()
    text = read_log_tail()
    stage, by, cur, last, fails, resume, stage_exit = parse_log_tail(text)
    procs = find_embed_processes()
    tgt = ms_target()
    ms_docs, ms_ch = cats["mehrsys"]
    print("\n" + "=" * 64, flush=True)
    print(f"  EMBED WATCH  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print("=" * 64, flush=True)
    status = f"  ({stage_exit})" if stage_exit else "  (running / loading)"
    print(f"  Active stage: {stage}{status}", flush=True)
    if procs:
        for p in procs:
            cpu = f"{p['cpu']:.0f}%" if p.get("cpu") is not None else "?"
            rss = f"{p['rss_mb']:.0f}MB" if p.get("rss_mb") is not None else "?"
            print(f"  Process   PID={p['pid']}  CPU={cpu}  RSS={rss}", flush=True)
    else:
        print("  Process   (no embed python found)", flush=True)
    print(f"  Mehrsys   {ms_docs:4d}/{tgt} books   {ms_ch:8d} chunks", flush=True)
    print(f"  Library   {cats['library'][0]:4d} docs      {cats['library'][1]:8d} chunks", flush=True)
    print(f"  Exam      {cats['exam'][0]:4d} docs      {cats['exam'][1]:8d} chunks", flush=True)
    print(f"  Standards {cats['standards'][0]:4d} docs      {cats['standards'][1]:8d} chunks", flush=True)
    print(f"  This stage (log tail): ok={sum(by.values())}  fail={fails}", flush=True)
    if resume:
        print(f"  {resume[:72]}", flush=True)
    if cur:
        t, d, tot = cur
        pct = 100.0 * d / tot if tot else 0
        bar = "#" * int(pct // 5) + "-" * (20 - int(pct // 5))
        print(f"  Current   [{bar}] {pct:5.1f}%  {t}  {d}/{tot}", flush=True)

    progress_stale = False
    max_age = None
    for book_id, done, total, updated in prog:
        pct = 100.0 * done / total if total else 0
        age = parse_updated_age(updated)
        if age is not None:
            max_age = age if max_age is None else max(max_age, age)
            if age > STALE_PROGRESS_SEC:
                progress_stale = True
        age_s = f"age={age:.0f}s" if age is not None else "age=?"
        print(
            f"  DB live   {done}/{total} ({pct:.1f}%)  "
            f"upd={updated[11:19] if updated else '?'} {age_s}  …{book_id[-50:]}",
            flush=True,
        )

    try:
        age = time.time() - LOG.stat().st_mtime
        print(f"  Log       {LOG.stat().st_size/1e6:.1f} MB  idle {age:.0f}s", flush=True)
    except OSError:
        pass

    if progress_stale and not procs:
        print(
            f"  WARNING   progress stale >{STALE_PROGRESS_SEC // 60} min "
            "AND no embed process — STUCK/crashed",
            flush=True,
        )
        print("  Restart:  python -u scripts/embed_all_live.py", flush=True)
        print("  Qdrant:   docker compose up -d   OR   .\\scripts\\start_qdrant.ps1", flush=True)
    elif progress_stale and procs:
        print(
            "  NOTE      progress DB stale but python still alive "
            "(model load / long chunk?)",
            flush=True,
        )
    elif not procs and not prog:
        print("  NOTE      no live progress and no embed process", flush=True)

    print("  DO NOT run validation while embedding (OOM).", flush=True)
    print("=" * 64, flush=True)


def main():
    import sys
    loop = "--loop" in sys.argv
    while True:
        once()
        if not loop:
            break
        time.sleep(30)


if __name__ == "__main__":
    main()
