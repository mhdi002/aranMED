import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from medrag.config import CATALOG_DB
from medrag.catalog.registry import scan_library
from medrag.index import vectorstore as vs

conn = sqlite3.connect(CATALOG_DB)
indexed = {r[0] for r in conn.execute("SELECT book_id FROM documents WHERE source_corpus='library'")}
exam = conn.execute("SELECT COUNT(*), COALESCE(SUM(n_chunks),0) FROM documents WHERE source_corpus='exam'").fetchone()
conn.close()

missing = []
for e in scan_library():
    p = Path(e["file_path"])
    bid = f"lib:{e['specialty']}:{p.stem}"
    if bid not in indexed:
        missing.append((e["title"], p.suffix.lower()))

print("exam_books", exam[0], "exam_chunks", exam[1])
print("qdrant_total", vs.count())
print("library_indexed", len(indexed), "missing", len(missing))
epub = [m for m in missing if m[1] == ".epub"]
print("missing_epub", len(epub))
for t, s in missing[:20]:
    print(f"  {s:5s} {t[:55]}")
