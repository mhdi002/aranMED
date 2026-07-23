"""Estimate OCR + pipeline remaining time."""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from medrag.config import MANIFEST, OCR_OUT

log = Path(__file__).resolve().parent.parent / "reports" / "overnight_pipeline.log"
text = log.read_bytes().decode("utf-8", errors="replace") if log.exists() else ""

manifest = [json.loads(l) for l in open(MANIFEST, encoding="utf-8")]
ocr_books = [r for r in manifest if r.get("needs_ocr") and not r.get("duplicate")]
print(f"OCR books total: {len(ocr_books)}")

# Parse log for page progress lines: p200/270 (35s)
pages_done_est = 0
books_touched = set()
for line in text.splitlines():
    m = re.search(r"p(\d+)/(\d+)", line)
    if m:
        pages_done_est = max(pages_done_est, 0)  # reset per book logic below
    m2 = re.search(r"\s+p(\d+)/(\d+)\s+\(", line)
    if m2:
        pass

# Count jsonl files and pages
total_pages_needed = 0
pages_in_jsonl = 0
complete_books = 0
partial_books = 0
not_started = 0
for rec in ocr_books:
    stem = Path(rec["file"]).stem
    out = OCR_OUT / f"{stem}.jsonl"
    import fitz
    try:
        n_pages = fitz.open(rec["path"]).page_count
    except Exception:
        n_pages = 200  # guess
    total_pages_needed += n_pages
    if not out.exists():
        not_started += 1
        continue
    done = sum(1 for _ in open(out, encoding="utf-8"))
    pages_in_jsonl += done
    if done >= n_pages:
        complete_books += 1
    else:
        partial_books += 1

remaining_pages = max(0, total_pages_needed - pages_in_jsonl)
# ~7 sec/page from log (35s per 5 pages)
sec_per_page = 7.0
ocr_hours = remaining_pages * sec_per_page / 3600

print(f"Complete OCR books: {complete_books}")
print(f"Partial OCR books:  {partial_books}")
print(f"Not started:        {not_started}")
print(f"Pages done/total:   {pages_in_jsonl}/{total_pages_needed} ({100*pages_in_jsonl/max(1,total_pages_needed):.1f}%)")
print(f"OCR ETA:            ~{ocr_hours:.1f} hours ({ocr_hours/24:.1f} days)")
print(f"After OCR:          ~45 min embed + ~45 min eval")
print(f"TOTAL ETA:          ~{ocr_hours + 1.5:.1f} hours")

# Current book from log tail
for line in reversed(text.splitlines()):
    if "p" in line and "/" in line and "s)" in line:
        print(f"Current:            {line.strip()[:80]}")
        break
