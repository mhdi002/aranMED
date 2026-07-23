"""Report which exam books are excluded from OCR vs kept."""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from medrag.config import MANIFEST

rows = [json.loads(l) for l in open(MANIFEST, encoding="utf-8")]
excl = [r for r in rows if r.get("excluded")]
ocr = [r for r in rows if r.get("needs_ocr")]

print("=== EXCLUDED (skip OCR) ===")
for reason in sorted({r.get("exclude_reason") for r in excl}):
    grp = [r for r in excl if r.get("exclude_reason") == reason]
    pages = sum(r["pages"] for r in grp)
    print(f"\n{reason}: {len(grp)} books, {pages} pages")
    for r in sorted(grp, key=lambda x: -x["pages"]):
        print(f"  [{r['type']:8}] {r['specialty']:10} {r['file'][:55]} ({r['pages']}p)")

print("\n=== KEEP OCR (FA qbank/exam + Iran-only textbooks) ===")
pages_ocr = sum(r["pages"] for r in ocr)
print(f"{len(ocr)} books, {pages_ocr} pages")
for r in sorted(ocr, key=lambda x: -x["pages"]):
    print(f"  [{r['type']:8}] {r['specialty']:10} {r['file'][:55]} ({r['pages']}p)")

print("\nKept FA by type:", dict(Counter(r["type"] for r in rows if r["language"] == "fa" and not r.get("excluded"))))
