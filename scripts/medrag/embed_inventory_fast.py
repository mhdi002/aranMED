"""Fast inventory without importing medrag (avoids Qdrant/heavy imports)."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from medrag.config import (  # noqa: E402
    CATALOG_DB, LIBRARY_DIR, MANIFEST, MEHRSYS_BOOKS_DIR, OCR_OUT, REPORTS_DIR,
    STANDARDS_EXTRACTED,
)

LIB_DIR = Path(LIBRARY_DIR)
MEHRSYS = Path(MEHRSYS_BOOKS_DIR) if MEHRSYS_BOOKS_DIR else Path()
STANDARDS = Path(STANDARDS_EXTRACTED)
CATALOG = Path(CATALOG_DB)
MANIFEST = Path(MANIFEST)
OCR_DIR = Path(OCR_OUT)
OUT = REPORTS_DIR / "embed_inventory.txt"
SUMMARY = REPORTS_DIR / "embed_inventory_summary.txt"


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def catalog_done(conn, book_id: str, content_hash: str) -> tuple[bool, int]:
    row = conn.execute(
        "SELECT content_hash, indexed_hash, n_chunks FROM documents WHERE book_id=?",
        (book_id,),
    ).fetchone()
    if not row:
        return False, 0
    ch, ih, n = row
    ok = bool(ih) and ch == content_hash and ih == content_hash and (n or 0) > 0
    return ok, (n or 0)


def scan_en_library() -> list[dict]:
    entries = []
    if not LIB_DIR.exists():
        return entries
    for specialty_dir in sorted(LIB_DIR.iterdir()):
        if not specialty_dir.is_dir():
            continue
        specialty = specialty_dir.name.replace("_", " ")
        for fp in sorted(specialty_dir.iterdir()):
            if fp.suffix.lower() not in (".pdf", ".epub"):
                continue
            entries.append({
                "title": fp.stem,
                "specialty": specialty,
                "file_path": fp,
                "language": "en",
                "source_corpus": "library",
            })
    return entries


def scan_mehrsys() -> list[dict]:
    entries = []
    if not MEHRSYS.exists():
        return entries
    for p in sorted(MEHRSYS.iterdir()):
        # packs are folders or known extensions
        if p.is_dir():
            entries.append({
                "title": p.name,
                "specialty": "mehrsys",
                "file_path": p,
                "language": "en",
                "source_corpus": "mehrsys",
            })
        elif p.suffix.lower() in (".pdf", ".epub", ".zip"):
            entries.append({
                "title": p.stem,
                "specialty": "mehrsys",
                "file_path": p,
                "language": "en",
                "source_corpus": "mehrsys",
            })
    return entries


def scan_standards() -> list[dict]:
    entries = []
    if not STANDARDS.exists():
        return entries
    for fp in STANDARDS.rglob("*.pdf"):
        entries.append({
            "title": fp.stem,
            "specialty": "standards",
            "file_path": fp,
            "language": "fa",
            "source_corpus": "standards",
        })
    return entries


def pack_content_hash(folder: Path) -> str:
    h = hashlib.sha256()
    files = sorted([p for p in folder.rglob("*") if p.is_file()], key=lambda p: str(p).lower())
    for p in files:
        h.update(str(p.relative_to(folder)).encode())
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    return h.hexdigest()


def main():
    print("open catalog", flush=True)
    conn = sqlite3.connect(str(CATALOG))
    rows = conn.execute(
        "SELECT source_corpus, language, title, n_chunks, book_id FROM documents"
    ).fetchall()

    lines = []
    by = Counter()
    chunks = Counter()
    by_lang = Counter()
    for corp, lang, title, n, bid in rows:
        corp = corp or "?"
        lang = (lang or "?").lower()
        by[corp] += 1
        chunks[corp] += n or 0
        by_lang[(corp, lang)] += 1

    lines.append("=== INDEXED IN CATALOG ===")
    for corp in sorted(by):
        lines.append(f"  {corp}: {by[corp]} docs, {chunks[corp]} chunks")
    lines.append("by corpus/language:")
    for k, v in sorted(by_lang.items()):
        lines.append(f"  {k[0]}/{k[1]}: {v} docs")

    print("scan EN library", LIB_DIR, flush=True)
    en = scan_en_library()
    print(f"  found {len(en)}", flush=True)
    print("scan mehrsys", flush=True)
    ms = scan_mehrsys()
    print(f"  found {len(ms)}", flush=True)
    print("scan standards", flush=True)
    st = scan_standards()
    print(f"  found {len(st)}", flush=True)

    embedded = defaultdict(list)
    pending = defaultdict(list)

    def check(entries, id_fn, hash_fn):
        for i, e in enumerate(entries):
            if i and i % 50 == 0:
                print(f"  {e['source_corpus']} {i}/{len(entries)}", flush=True)
            path = e["file_path"]
            title = e["title"]
            key = f"{e['source_corpus']}/{e['language']}"
            try:
                ch = hash_fn(path)
                bid = id_fn(e, path)
                ok, n = catalog_done(conn, bid, ch)
            except Exception as ex:
                ok, n = False, 0
                title = f"{title} (error:{type(ex).__name__})"
            if ok:
                embedded[key].append((title, n))
            else:
                pending[key].append(title)

    check(
        en,
        lambda e, p: f"lib:{e['specialty']}:{p.stem}",
        hash_file,
    )
    check(
        ms,
        lambda e, p: f"mehrsys:{e['specialty']}:{p.name if p.is_dir() else p.stem}",
        lambda p: pack_content_hash(p) if p.is_dir() else hash_file(p),
    )
    check(
        st,
        lambda e, p: f"std:{e['specialty']}:{p.stem}",
        hash_file,
    )

    # Also match library books that are in catalog under slightly different book_id
    # by comparing titles for any leftover pending
    catalog_en_titles = {
        (r[2] or "").lower()
        for r in rows
        if r[0] == "library" and (r[3] or 0) > 0
    }

    lines.append(f"\n=== DISK SCAN ===")
    lines.append(f"  library/en files: {len(en)}")
    lines.append(f"  mehrsys files: {len(ms)}")
    lines.append(f"  standards pdfs: {len(st)}")

    lines.append("\n=== SUMMARY (hash-matched) ===")
    for key in sorted(set(list(embedded) + list(pending))):
        lines.append(
            f"  {key}: embedded={len(embedded.get(key, []))} pending={len(pending.get(key, []))}"
        )

    # Title-based fallback for English library (legacy book_ids)
    title_pending = []
    title_ok = []
    for e in en:
        if e["title"].lower() in catalog_en_titles:
            title_ok.append(e["title"])
        else:
            title_pending.append(e["title"])
    lines.append("\n=== ENGLISH LIBRARY (title match vs catalog) ===")
    lines.append(f"  on disk: {len(en)}")
    lines.append(f"  in catalog with chunks: {len(title_ok)}")
    lines.append(f"  missing from catalog: {len(title_pending)}")

    # Catalog library titles not on disk
    disk_titles = {e["title"].lower() for e in en}
    orphan = [
        r[2] for r in rows
        if r[0] == "library" and (r[3] or 0) > 0 and (r[2] or "").lower() not in disk_titles
    ]
    lines.append(f"  in catalog but not on disk now: {len(orphan)}")

    for key in sorted(set(list(embedded) + list(pending))):
        lines.append(f"\n===== EMBEDDED {key} ({len(embedded.get(key, []))}) =====")
        for t, n in sorted(embedded.get(key, []), key=lambda x: x[0].lower()):
            lines.append(f"  [ok] {t}  ({n} chunks)")
        lines.append(f"\n===== PENDING {key} ({len(pending.get(key, []))}) =====")
        for t in sorted(pending.get(key, []), key=str.lower):
            lines.append(f"  [pending] {t}")

    lines.append("\n===== EN LIBRARY MISSING (title not in catalog) =====")
    for t in sorted(title_pending, key=str.lower):
        lines.append(f"  [pending] {t}")

    lines.append("\n===== EN LIBRARY IN CATALOG BUT FILE MISSING =====")
    for t in sorted(orphan, key=lambda x: (x or "").lower()):
        lines.append(f"  [orphan] {t}")

    # Exam
    lines.append("\n=== EXAM ===")
    exam_docs = [r for r in rows if r[0] == "exam"]
    lines.append(f"  catalog: {len(exam_docs)} docs, {sum(r[3] or 0 for r in exam_docs)} chunks")
    exam_ok, exam_pend, exam_nopages = [], [], []
    if MANIFEST.exists():
        for line in MANIFEST.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("duplicate") or rec.get("excluded"):
                continue
            stem = os.path.splitext(rec["file"])[0]
            pages_file = OCR_DIR / f"{stem}.jsonl"
            pages_dir = OCR_DIR / stem
            has_pages = pages_file.exists() or pages_dir.exists()
            if not has_pages:
                # also check alternate patterns
                alts = list(OCR_DIR.glob(f"{stem}*"))
                has_pages = bool(alts)
            bid = f"exam:{stem}"
            row = conn.execute(
                "SELECT n_chunks FROM documents WHERE book_id=? OR title=?",
                (bid, rec["file"]),
            ).fetchone()
            if row and (row[0] or 0) > 0:
                exam_ok.append(rec["file"])
            elif not has_pages:
                exam_nopages.append(rec["file"])
            else:
                exam_pend.append(rec["file"])
    lines.append(f"  manifest embedded: {len(exam_ok)}")
    lines.append(f"  manifest pending (has OCR?): {len(exam_pend)}")
    lines.append(f"  manifest no OCR: {len(exam_nopages)}")
    lines.append(f"\n===== EMBEDDED exam ({len(exam_ok)}) =====")
    for t in sorted(exam_ok, key=str.lower):
        lines.append(f"  [ok] {t}")
    lines.append(f"\n===== PENDING exam ({len(exam_pend)}) =====")
    for t in sorted(exam_pend, key=str.lower):
        lines.append(f"  [pending] {t}")
    lines.append(f"\n===== NO OCR exam ({len(exam_nopages)}) =====")
    for t in sorted(exam_nopages, key=str.lower):
        lines.append(f"  [no-ocr] {t}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = [
        "=== INDEXED IN CATALOG ===",
        *[f"  {c}: {by[c]} docs, {chunks[c]} chunks" for c in sorted(by)],
        "by corpus/language:",
        *[f"  {a}/{b}: {n} docs" for (a, b), n in sorted(by_lang.items())],
        "",
        f"EN library on disk: {len(en)}",
        f"EN library in catalog (title match): {len(title_ok)}",
        f"EN library MISSING: {len(title_pending)}",
        f"Mehrsys on disk: {len(ms)} | embedded(hash)={len(embedded.get('mehrsys/en',[]))} pending={len(pending.get('mehrsys/en',[]))}",
        f"Standards on disk: {len(st)} | embedded(hash)={len(embedded.get('standards/fa',[]))} pending={len(pending.get('standards/fa',[]))}",
        f"Exam catalog: {len(exam_docs)} | manifest ok={len(exam_ok)} pending={len(exam_pend)} no-ocr={len(exam_nopages)}",
        "",
        "--- EN LIBRARY MISSING ---",
        *[f"  [pending] {t}" for t in sorted(title_pending, key=str.lower)],
        "",
        "--- MEHRSYS PENDING ---",
        *[f"  [pending] {t}" for t in sorted(pending.get("mehrsys/en", []), key=str.lower)],
        "",
        "--- STANDARDS PENDING ---",
        *[f"  [pending] {t}" for t in sorted(pending.get("standards/fa", []), key=str.lower)],
        "",
        "--- EXAM PENDING ---",
        *[f"  [pending] {t}" for t in sorted(exam_pend, key=str.lower)],
        "",
        "--- EXAM NO OCR ---",
        *[f"  [no-ocr] {t}" for t in sorted(exam_nopages, key=str.lower)],
        "",
        f"Full inventory: {OUT}",
    ]
    SUMMARY.write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n".join(summary), flush=True)


if __name__ == "__main__":
    main()
