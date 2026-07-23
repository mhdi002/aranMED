"""Quick embed progress estimate."""
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from medrag.config import CATALOG_DB
from medrag.catalog.registry import scan_library
import sqlite3

log = ROOT / "reports" / "embed_library.log"
text = log.read_bytes().decode("utf-8", errors="replace") if log.exists() else ""
# handle possible bracket variants
text = text.replace("\uff3b", "[").replace("\uff3d", "]")

ok = len(re.findall(r"\[ok\]\s+lib", text))
fail = len(re.findall(r"\[fail\]", text))
skip = len(re.findall(r"\[skip\]", text))

total = len(list(scan_library()))
conn = sqlite3.connect(CATALOG_DB)
row = conn.execute(
    "SELECT COUNT(*), COALESCE(SUM(n_chunks),0) FROM documents WHERE source_corpus='library'"
).fetchone()
time_row = (None, None)
try:
    time_row = conn.execute(
        "SELECT MIN(updated_at), MAX(updated_at) FROM documents WHERE source_corpus='library'"
    ).fetchone()
except sqlite3.OperationalError:
    pass
try:
    prog = conn.execute("SELECT COUNT(*) FROM embed_progress").fetchone()[0]
except sqlite3.OperationalError:
    prog = 0
conn.close()
catalog_books, catalog_chunks = row

done_log = ok + skip
remaining = max(0, total - catalog_books - fail)

lines = [l for l in text.splitlines() if "[ok] lib" in l or "[fail]" in l]
last_lines = lines[-3:]

mtime = datetime.fromtimestamp(log.stat().st_mtime) if log.exists() else None

print(f"Library PDFs total:     {total}")
print(f"Log [ok] lib:           {ok}")
print(f"Log [skip]:             {skip}")
print(f"Log [fail]:             {fail}")
print(f"Catalog indexed (lib):  {catalog_books} books, {catalog_chunks} chunks")
print(f"Remaining (~):          {remaining} books (+ {fail} to retry)")
if total:
    print(f"Progress (catalog):     {catalog_books/total*100:.1f}%")
if mtime:
    print(f"Log last updated:       {mtime.strftime('%Y-%m-%d %H:%M:%S')}")
if time_row[0]:
    print(f"First indexed at:       {str(time_row[0])[:19]}")
    print(f"Last indexed at:        {str(time_row[1])[:19]}")
    try:
        from datetime import datetime as dt
        t0 = dt.fromisoformat(str(time_row[0]).replace("Z", "+00:00"))
        t1 = dt.fromisoformat(str(time_row[1]).replace("Z", "+00:00"))
        hours = max((t1 - t0).total_seconds() / 3600, 0.1)
        rate = catalog_books / hours
        rem_h = remaining / rate if rate > 0 else 0
        print(f"Rate:                   ~{rate:.1f} books/hour")
        print(f"ETA (rough):              ~{rem_h:.1f} hours ({rem_h/24:.1f} days)")
    except Exception:
        pass
elif log.exists():
    import os
    from datetime import datetime as dt
    started = dt.fromtimestamp(os.path.getctime(log))
    elapsed_h = max((datetime.now() - started).total_seconds() / 3600, 0.1)
    rate = catalog_books / elapsed_h
    rem_h = remaining / rate if rate > 0 else 0
    print(f"Log started:            {started.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Elapsed:                ~{elapsed_h:.1f} hours")
    print(f"Rate:                   ~{rate:.1f} books/hour")
    print(f"ETA (rough):              ~{rem_h:.1f} hours ({rem_h/24:.1f} days)")
print(f"In-progress (partial):  {prog} books")
if last_lines:
    print("Recent:")
    for l in last_lines:
        print(" ", l[:100])
