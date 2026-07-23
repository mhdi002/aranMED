"""Inventory embedded vs pending using catalog.db only (no Qdrant load)."""
from __future__ import annotations

import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

from medrag.catalog.documents import hash_file
from medrag.catalog.registry import scan_library
from medrag.ingest.mehrsys import is_mehrsys_pack, pack_content_hash, pack_dir

OUT = ROOT / "reports" / "embed_inventory.txt"
SUMMARY = ROOT / "reports" / "embed_inventory_summary.txt"


def catalog_done(conn, book_id: str, content_hash: str) -> bool:
    row = conn.execute(
        "SELECT content_hash, indexed_hash, n_chunks FROM documents WHERE book_id=?",
        (book_id,),
    ).fetchone()
    if not row:
        return False
    ch, ih, n = row
    return bool(ih) and ch == content_hash and ih == content_hash and (n or 0) > 0


def main():
    conn = sqlite3.connect("catalog.db")
    rows = conn.execute(
        "SELECT source_corpus, language, title, n_chunks, book_id FROM documents"
    ).fetchall()

    lines: list[str] = []
    by_corpus = Counter()
    by_lang = Counter()
    chunks_by = Counter()
    for corp, lang, title, n, bid in rows:
        corp = corp or "?"
        lang = (lang or "?").lower()
        by_corpus[corp] += 1
        by_lang[(corp, lang)] += 1
        chunks_by[corp] += n or 0

    lines.append("=== INDEXED IN CATALOG ===")
    for corp in sorted(by_corpus):
        lines.append(f"  {corp}: {by_corpus[corp]} docs, {chunks_by[corp]} chunks")
    lines.append("by corpus/language:")
    for (corp, lang), n in sorted(by_lang.items()):
        lines.append(f"  {corp}/{lang}: {n} docs")

    print("Scanning library...", flush=True)
    scan = list(scan_library())
    lines.append(f"\n=== LIBRARY SCAN total: {len(scan)} ===")
    scan_by = Counter(e.get("source_corpus", "library") for e in scan)
    scan_lang = Counter(
        (e.get("source_corpus", "library"), (e.get("language") or "?").lower())
        for e in scan
    )
    for k, v in sorted(scan_by.items()):
        lines.append(f"  scan {k}: {v}")
    for (corp, lang), v in sorted(scan_lang.items()):
        lines.append(f"  scan {corp}/{lang}: {v}")

    embedded: dict[str, list[tuple[str, int]]] = defaultdict(list)
    pending: dict[str, list[str]] = defaultdict(list)

    for i, e in enumerate(scan):
        if i and i % 100 == 0:
            print(f"  checked {i}/{len(scan)}", flush=True)
        corp = e.get("source_corpus", "library")
        lang = (e.get("language") or "?").lower()
        path = Path(e["file_path"])
        title = e.get("title") or path.stem
        try:
            if is_mehrsys_pack(path):
                folder = pack_dir(path)
                content_hash = pack_content_hash(folder)
                book_id = f"mehrsys:{e['specialty']}:{folder.name}"
            elif corp == "standards":
                content_hash = hash_file(path)
                book_id = f"std:{e['specialty']}:{path.stem}"
            else:
                content_hash = hash_file(path)
                book_id = f"lib:{e['specialty']}:{path.stem}"
            done = catalog_done(conn, book_id, content_hash)
            n = 0
            if done:
                n = conn.execute(
                    "SELECT n_chunks FROM documents WHERE book_id=?", (book_id,)
                ).fetchone()[0]
        except Exception as ex:
            done = False
            title = f"{title} (error: {type(ex).__name__})"
            n = 0
        key = f"{corp}/{lang}"
        if done:
            embedded[key].append((title, n or 0))
        else:
            pending[key].append(title)

    lines.append("\n=== SUMMARY EMBEDDED vs PENDING (scan vs catalog) ===")
    for key in sorted(set(list(embedded) + list(pending))):
        lines.append(
            f"  {key}: embedded={len(embedded.get(key, []))} "
            f"pending={len(pending.get(key, []))}"
        )

    for key in sorted(set(list(embedded) + list(pending))):
        lines.append(f"\n===== EMBEDDED {key} ({len(embedded.get(key, []))}) =====")
        for t, n in sorted(embedded.get(key, []), key=lambda x: x[0].lower()):
            lines.append(f"  [ok] {t}  ({n} chunks)")
        lines.append(f"\n===== PENDING {key} ({len(pending.get(key, []))}) =====")
        for t in sorted(pending.get(key, []), key=str.lower):
            lines.append(f"  [pending] {t}")

    # Exam
    lines.append("\n=== EXAM CORPUS ===")
    exam_rows = [r for r in rows if r[0] == "exam"]
    lines.append(
        f"  catalog exam docs: {len(exam_rows)}, "
        f"chunks: {sum(r[3] or 0 for r in exam_rows)}"
    )
    try:
        from medrag.ingest.exam_ocr import exam_content_hash, load_manifest, load_pages

        man = load_manifest()
        exam_ok, exam_pend, exam_nopages = [], [], []
        for rec in man:
            if rec.get("duplicate") or rec.get("excluded"):
                continue
            stem = os.path.splitext(rec["file"])[0]
            if not load_pages(stem):
                exam_nopages.append(rec["file"])
                continue
            ch = exam_content_hash(stem, rec.get("path"))
            bid = f"exam:{stem}"
            if catalog_done(conn, bid, ch):
                exam_ok.append(rec["file"])
            else:
                hit = conn.execute(
                    "SELECT n_chunks FROM documents WHERE source_corpus='exam' AND "
                    "(title=? OR book_id=?)",
                    (rec["file"], bid),
                ).fetchone()
                if hit and (hit[0] or 0) > 0:
                    exam_ok.append(rec["file"])
                else:
                    exam_pend.append(rec["file"])
        lines.append(f"  manifest embedded: {len(exam_ok)}")
        lines.append(f"  manifest pending (OCR done, not indexed): {len(exam_pend)}")
        lines.append(f"  manifest no OCR pages: {len(exam_nopages)}")
        lines.append(f"\n===== EMBEDDED exam ({len(exam_ok)}) =====")
        for t in sorted(exam_ok, key=str.lower):
            lines.append(f"  [ok] {t}")
        lines.append(f"\n===== PENDING exam ({len(exam_pend)}) =====")
        for t in sorted(exam_pend, key=str.lower):
            lines.append(f"  [pending] {t}")
        lines.append(f"\n===== NO OCR exam ({len(exam_nopages)}) =====")
        for t in sorted(exam_nopages, key=str.lower):
            lines.append(f"  [no-ocr] {t}")
    except Exception as ex:
        lines.append(f"  exam check failed: {type(ex).__name__}: {ex}")

    text = "\n".join(lines) + "\n"
    OUT.write_text(text, encoding="utf-8")

    # Compact summary for stdout
    summary = []
    for line in lines:
        if (
            line.startswith("===")
            or line.startswith("  ")
            and (
                ":" in line
                and (
                    "docs" in line
                    or "scan " in line
                    or "embedded=" in line
                    or "manifest" in line
                    or "catalog exam" in line
                )
            )
        ):
            summary.append(line)
    summary.append("")
    summary.append("--- PENDING English library books ---")
    for t in sorted(pending.get("library/en", []), key=str.lower):
        summary.append(f"  [pending] {t}")
    summary.append("")
    summary.append("--- PENDING Mehrsys ---")
    for key in sorted(k for k in pending if k.startswith("mehrsys")):
        summary.append(f"{key} ({len(pending[key])}):")
        for t in sorted(pending[key], key=str.lower):
            summary.append(f"  [pending] {t}")
    summary.append("")
    summary.append(f"Full lists: {OUT}")
    SUMMARY.write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n".join(summary), flush=True)


if __name__ == "__main__":
    main()
