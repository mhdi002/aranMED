"""Export undownloaded titles from catalog.db to a text file."""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from medrag.config import CATALOG_DB, REPORTS_DIR  # noqa: E402

DB = Path(sys.argv[1]) if len(sys.argv) > 1 else CATALOG_DB
OUT = REPORTS_DIR / "not_downloaded_books.txt"


def main() -> None:
    if not DB.exists():
        raise SystemExit(f"catalog not found: {DB}")
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        """
        SELECT specialty, kind, title, status, fail_reason
        FROM titles
        WHERE status IS NULL OR status NOT IN ('downloaded', 'skipped')
        ORDER BY specialty COLLATE NOCASE, kind DESC, title COLLATE NOCASE
        """
    ).fetchall()
    by_status = Counter(r[3] or "unknown" for r in rows)
    by_spec = Counter(r[0] for r in rows)
    n_all = conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0]
    n_dl = conn.execute(
        "SELECT COUNT(*) FROM titles WHERE status='downloaded'"
    ).fetchone()[0]
    n_skip = conn.execute(
        "SELECT COUNT(*) FROM titles WHERE status='skipped'"
    ).fetchone()[0]
    conn.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"Not-downloaded medical books (from specialty .docx catalog)",
        f"Generated: {stamp}",
        f"Catalog DB: {DB}",
        f"Total titles in catalog: {n_all}",
        f"Downloaded: {n_dl}",
        f"Skipped: {n_skip}",
        f"Not downloaded (this list): {len(rows)}",
        f"  by status: {dict(by_status)}",
        "",
        "=" * 80,
        "SUMMARY BY SPECIALTY",
        "=" * 80,
    ]
    for spec, n in sorted(by_spec.items(), key=lambda x: (-x[1], x[0].lower())):
        lines.append(f"  {n:4d}  {spec}")

    lines += ["", "=" * 80, "FULL LIST", "=" * 80, ""]

    current = None
    for specialty, kind, title, status, fail_reason in rows:
        if specialty != current:
            current = specialty
            lines.append("")
            lines.append(f"## {specialty}")
            lines.append("-" * 60)
        reason = (fail_reason or "").replace("\n", " ")[:120]
        kind_s = kind or "?"
        status_s = status or "?"
        if reason:
            lines.append(f"[{status_s:9}] [{kind_s:10}] {title}  |  {reason}")
        else:
            lines.append(f"[{status_s:9}] [{kind_s:10}] {title}")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(rows)} titles -> {OUT}")


if __name__ == "__main__":
    main()
