"""Quick health check for embed run."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
log = (ROOT / "reports" / "embed_all.log").read_text(encoding="utf-8", errors="replace")

print("=== PROCESS / LOG TAIL ===")
# stage exits
for m in re.finditer(r"=== (\w+) (start|exit) ([^\n]*)", log):
    pass
exits = re.findall(r"=== (\w+) exit ([^\n]+)", log)
print("stage exits:", exits[-6:])
starts = re.findall(r"=== (\w+) start ([^\n]+)", log)
print("stage starts:", starts[-4:])

run = log.split("=== mehrsys start 2026-07-17T12:20")[-1]
ok_ms = len(re.findall(r"\[ok\]\s+mehrsys", run))
fail_ms = len(re.findall(r"^\[fail\]", run, re.M))
print(f"mehrsys this-run: ok={ok_ms} fail={fail_ms}")

# crash markers after 12:20 run
crash = re.findall(r"(MemoryError|IndexError|Traceback|exit 3221225477|Access)", run)
print("crash markers in this-run:", Counter := __import__("collections").Counter(crash))

print("\nLast 15 ok/fail/exit lines:")
lines = [l for l in log.splitlines() if l.startswith("[ok]") or l.startswith("[fail]") or l.startswith("===")]
for l in lines[-15:]:
    print(" ", l[:110])

conn = sqlite3.connect(ROOT / "catalog.db")
ms = conn.execute(
    "select count(1), coalesce(sum(n_chunks),0) from documents where source_corpus='mehrsys'"
).fetchone()
prog = conn.execute(
    "select book_id, chunks_done, chunks_total from embed_progress"
).fetchall()
print(f"\ncatalog mehrsys: {ms[0]} books, {ms[1]} chunks")
print(f"embed_progress rows: {len(prog)}")
for p in prog[:8]:
    print(" ", p)
conn.close()

# current stage hint
if "=== library start" in log.split("=== mehrsys exit")[-1]:
    print("\nSTATUS: Mehrsys stage ended; library stage was started.")
if "3221225477" in log:
    print("NOTE: exit 3221225477 = Windows access violation crash (not a clean finish).")
    print("Books already [ok] are kept; crashed book can resume from embed_progress.")
