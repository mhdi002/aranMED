"""Summarize Mehrsys book pack types and text volume."""
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from medrag.config import MEHRSYS_BOOKS_DIR  # noqa: E402

base = Path(MEHRSYS_BOOKS_DIR) if MEHRSYS_BOOKS_DIR else None
if not base or not base.exists():
    raise SystemExit(
        "Mehrsys dir not set/found. Set MEDRAG_MEHRSYS_DIR or paths.mehrsys_books_dir"
    )
types = Counter()
ok = 0
bad = 0
chars = 0
samples = []
for d in sorted(base.iterdir()):
    if not d.is_dir():
        continue
    info_p = d / "info.json"
    fts_p = d / "fts.db"
    if not info_p.exists() or not fts_p.exists():
        bad += 1
        continue
    try:
        info = json.loads(info_p.read_text(encoding="utf-8"))
        types[info.get("type", "?")] += 1
        c = sqlite3.connect(str(fts_p))
        rows = c.execute("SELECT title, content FROM fts").fetchall()
        c.close()
        book_chars = sum(len(t or "") + len(html or "") for t, html in rows)
        chars += book_chars
        ok += 1
        if len(samples) < 5:
            samples.append((info.get("title"), info.get("type"), len(rows), book_chars))
    except Exception as e:
        bad += 1
        print("ERR", d.name, e)

print("books_ok", ok, "bad", bad)
print("types", dict(types))
print("total_html_chars", chars, "avg", chars // max(ok, 1))
for s in samples:
    print(" sample", s)
