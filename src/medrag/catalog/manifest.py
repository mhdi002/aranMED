"""Build manifest of exam/Persian PDFs from the exam books directory."""
from __future__ import annotations

import glob
import json
import os
import re
from collections import Counter
from pathlib import Path

import fitz

from medrag.config import CFG, EXAM_BOOKS_DIR, MANIFEST
from medrag.catalog.coverage import fa_textbook_redundant

EXAM_SKIP_EN = CFG.get("exam", {}).get("skip_en_books", True)
EXAM_SKIP_FA_REDUNDANT = CFG.get("exam", {}).get("skip_fa_textbook_if_en_covers", True)

SPECIALTY = {
    "cardio": ["قلب", "عروق", "cardio", "ekg", "ecg", "نعمتی"],
    "surgery": ["جراحی", "لارنس", "surg", "surgery"],
    "internal": ["داخلی", "harrison", "cecil", "internal"],
    "pediatrics": ["اطفال", "کودک", "nelson", "pediatr", "مانا"],
    "obgyn": ["زنان", "مامایی", "obstet", "beckmann", "ling"],
    "ortho": ["ارتوپدی", "شکستگی", "ortho", "اعلمی"],
    "derm": ["پوست", "derm", "skin"],
    "psychiatry": ["روان", "psychiat", "kaplan", "احمدی روان"],
    "neuro": ["مغز", "اعصاب", "neuro", "aminoff"],
    "pharm": ["فارماکولوژی", "دارو", "pharm", "katzung"],
    "patho": ["پاتولوژی", "patho", "robbins"],
    "radiology": ["رادیولوژی", "radiolog", "harring", "learning_radiology"],
    "urology": ["اورولوژی", "urolog", "سیم فروش"],
    "ent": ["گوش", "حلق", "بینی", "otolaryng", "goldenberg", "ent"],
    "ophtho": ["چشم", "ophtha", "جوادی"],
    "endo": ["غدد", "endo"],
    "nephro": ["کلیه", "nephro", "renal"],
    "gi": ["گوارش", "کبد", "gastro", "hepat"],
    "pulmo": ["ریه", "سیسیل", "pulmo", "respir"],
    "rheum": ["روماتولوژی", "rheum"],
    "heme_onc": ["خون", "انکولوژی", "hemat", "oncol"],
    "infectious": ["عفونی", "infect"],
    "epi_stats": ["اپیدمیولوژی", "آمار", "epidemi", "statist", "petrie", "sabin"],
    "ethics": ["اخلاق", "ملاحظات اخلاقی", "ethic"],
    "immunology": ["ایمن", "immun", "واکسن", "vaccine"],
}

_WATERMARK = re.compile(r"tabadol|جزوات|t\.me|telegram|@", re.IGNORECASE)


def doc_type(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ["qb", "کیوبی", "کوئسشن", "question", "qa", "بانک"]):
        return "qbank"
    if any(k in n for k in ["آزمون", "ارتقا", "بورد", "پرانترنی", "پره انترنی", "دستیاری", "board", "review"]):
        return "exam"
    return "textbook"


def guess_specialty(name: str) -> str:
    n = name.lower()
    for tag, kws in SPECIALTY.items():
        if any(k.lower() in n for k in kws):
            return tag
    return "general"


def language(name: str) -> str:
    ascii_ratio = sum(c.isascii() for c in name) / max(len(name), 1)
    return "en" if ascii_ratio > 0.7 else "fa"


def _clean_len(text: str) -> int:
    kept = [ln for ln in text.splitlines() if ln.strip() and not _WATERMARK.search(ln)]
    return len("".join(kept).strip())


def has_text_layer(path: str, sample_pages=10) -> bool:
    try:
        d = fitz.open(path)
    except Exception:
        return False
    n = d.page_count
    if n == 0:
        return False
    start = min(9, n // 3)
    best = 0
    for i in range(start, min(start + sample_pages, n)):
        best = max(best, _clean_len(d[i].get_text("text")))
    d.close()
    return best > 400


def _exclusion_reason(r: dict, all_rows: list[dict]) -> str | None:
    if r.get("duplicate"):
        return "duplicate_edition"
    if EXAM_SKIP_EN and r["language"] == "en":
        return "excluded_en_use_library"
    if EXAM_SKIP_FA_REDUNDANT and fa_textbook_redundant(r, all_rows):
        return "excluded_fa_textbook_en_covers_topic"
    return None


def build_exam_manifest(books_dir: Path | None = None) -> list[dict]:
    books_dir = books_dir or EXAM_BOOKS_DIR
    pdfs = sorted(glob.glob(str(books_dir / "*.pdf")))
    rows = []
    for p in pdfs:
        name = os.path.basename(p)
        try:
            pages = fitz.open(p).page_count
        except Exception:
            pages = -1
        txt = has_text_layer(p)
        rows.append({
            "file": name,
            "path": os.path.abspath(p),
            "language": language(name),
            "type": doc_type(name),
            "specialty": guess_specialty(name),
            "pages": pages,
            "has_text_layer": txt,
            "needs_ocr": not txt,
            "source_corpus": "exam",
        })
    seen = {}
    for r in rows:
        key = (re.sub(r"\s*\(\d+\)\s*", "", r["file"].lower()), r["pages"])
        if key in seen:
            r["duplicate"] = True
        else:
            r["duplicate"] = False
            seen[key] = r["file"]
        reason = _exclusion_reason(r, rows)
        r["excluded"] = reason is not None
        r["exclude_reason"] = reason
        if r["excluded"]:
            r["needs_ocr"] = False
    return rows


def write_manifest(rows: list[dict], out: Path | None = None) -> Path:
    out = out or MANIFEST
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return out


def main():
    rows = build_exam_manifest()
    write_manifest(rows)
    en = [r for r in rows if r["language"] == "en"]
    fa = [r for r in rows if r["language"] == "fa"]
    ocr = [r for r in rows if r["needs_ocr"]]
    print(f"Books: {len(rows)} | EN: {len(en)} | FA: {len(fa)} | Need OCR: {len(ocr)}")
    for tag, c in Counter(r["specialty"] for r in rows).most_common():
        print(f"  {tag:12s} {c}")
    print(f"Wrote {MANIFEST}")


if __name__ == "__main__":
    main()
