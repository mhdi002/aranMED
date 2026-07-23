"""Quick catalog download stats."""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from medrag.config import CATALOG_DB  # noqa: E402

db = Path(sys.argv[1]) if len(sys.argv) > 1 else CATALOG_DB
if not db.exists():
    raise SystemExit(f"catalog not found: {db}")

c = sqlite3.connect(db)
print(f"DB: {db}")
print("status:", dict(c.execute("SELECT status, COUNT(*) FROM titles GROUP BY status").fetchall()))
print("kind:", dict(c.execute("SELECT kind, COUNT(*) FROM titles GROUP BY kind").fetchall()))
print("pending books:", c.execute("SELECT COUNT(*) FROM titles WHERE status='pending' AND kind='book'").fetchone()[0])
print("failed books:", c.execute("SELECT COUNT(*) FROM titles WHERE status='failed' AND kind='book'").fetchone()[0])
