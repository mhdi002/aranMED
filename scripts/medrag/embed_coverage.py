"""Accurate unique-title coverage stats (paths from medrag.config)."""
from collections import Counter
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from medrag.config import (  # noqa: E402
    CATALOG_DB, LIBRARY_DIR, MEHRSYS_BOOKS_DIR, REPORTS_DIR, STANDARDS_EXTRACTED,
)

conn = sqlite3.connect(str(CATALOG_DB))

cat = conn.execute(
    "SELECT title, specialty, n_chunks, language, book_id "
    "FROM documents WHERE source_corpus='library'"
).fetchall()
cat_titles = {(t or "").lower() for t, _, n, _, _ in cat if (n or 0) > 0}
print("catalog library docs", len(cat))
print("catalog library with chunks", sum(1 for _, _, n, _, _ in cat if (n or 0) > 0))
print("catalog unique titles", len(cat_titles))
print("total library chunks", sum(n or 0 for _, _, n, _, _ in cat))

files = []
lib = Path(LIBRARY_DIR)
if lib.exists():
    for d in lib.iterdir():
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.suffix.lower() in (".pdf", ".epub"):
                files.append((f.stem, d.name, f))
print("disk files", len(files))
disk_titles = Counter(t.lower() for t, _, __ in files)
print("disk unique titles", len(disk_titles))
print("disk duplicate-title extras", len(files) - len(disk_titles))

missing = sorted(t for t in disk_titles if t not in cat_titles)
present = sorted(t for t in disk_titles if t in cat_titles)
print("unique titles embedded", len(present))
print("unique titles NOT embedded", len(missing))

std_ids = conn.execute(
    "SELECT book_id, title, n_chunks FROM documents WHERE source_corpus='standards'"
).fetchall()
cat_std = set()
for bid, title, n in std_ids:
    if (n or 0) > 0:
        cat_std.add(bid.split(":")[-1].lower())
        cat_std.add((title or "").lower())
std_root = Path(STANDARDS_EXTRACTED)
std_disk = list(std_root.rglob("*.pdf")) if std_root.exists() else []
std_pending = [p.stem for p in std_disk if p.stem.lower() not in cat_std]
print("standards catalog docs", len(std_ids), "chunks", sum(n or 0 for _, _, n in std_ids))
print("standards pdfs on disk", len(std_disk))
print("standards pending", len(std_pending))
for t in sorted(std_pending)[:40]:
    print("  [std pending]", t)

ex = conn.execute(
    "SELECT language, COUNT(1), SUM(n_chunks) FROM documents "
    "WHERE source_corpus='exam' GROUP BY language"
).fetchall()
print("exam by lang", ex)
print(
    "mehrsys in catalog",
    conn.execute(
        "SELECT COUNT(1) FROM documents WHERE source_corpus='mehrsys'"
    ).fetchone()[0],
)

ms = 0
if MEHRSYS_BOOKS_DIR and Path(MEHRSYS_BOOKS_DIR).exists():
    for p in Path(MEHRSYS_BOOKS_DIR).iterdir():
        if p.is_dir() and (p / "info.json").exists() and (p / "fts.db").exists():
            ms += 1
print("mehrsys packs on disk", ms)

REPORTS_DIR.mkdir(parents=True, exist_ok=True)
(REPORTS_DIR / "en_library_missing.txt").write_text(
    "\n".join(missing) + "\n", encoding="utf-8"
)
(REPORTS_DIR / "en_library_embedded.txt").write_text(
    "\n".join(present) + "\n", encoding="utf-8"
)
print("wrote", REPORTS_DIR / "en_library_missing.txt")
conn.close()
