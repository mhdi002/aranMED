"""Quick quality check: do Mehrsys packs contain real medical text?"""
import json
import sqlite3
from pathlib import Path

from medrag.ingest.mehrsys import extract_sections, html_to_text, scan_mehrsys_dir
from medrag.config import MEHRSYS_BOOKS_DIR

# Sample one of each type + a few well-known titles
WANT = [
    "Abeloff",
    "Braunwald",
    "Nelson",
    "Robbins",
    "Harrison",  # may be access-medicine
    "Williams Obstetrics",
    "Tintinalli",
]

base = Path(MEHRSYS_BOOKS_DIR)
entries = scan_mehrsys_dir(base)
by_type = {}
for e in entries:
    t = e.get("mehrsys_type") or "?"
    by_type.setdefault(t, []).append(e)

print(f"Total packs: {len(entries)}")
print("By type:", {k: len(v) for k, v in by_type.items()})
print()

checked = []
# one per type
for t, lst in by_type.items():
    checked.append(lst[0])
# named samples
for needle in WANT:
    for e in entries:
        if needle.lower() in e["title"].lower() and e not in checked:
            checked.append(e)
            break

print("=" * 72)
for e in checked[:12]:
    secs = extract_sections(Path(e["file_path"]))
    chars = sum(len(t) for _, _, t in secs)
    # sample a mid section with real body text
    body = ""
    for _i, title, text in secs:
        if len(text) > 800 and title.lower() not in ("front matter", "index", "copyright"):
            body = text
            break
    if not body and secs:
        body = secs[min(3, len(secs) - 1)][2]
    preview = " ".join(body.split())[:350]
    print(f"TITLE: {e['title']}")
    print(f"  type={e.get('mehrsys_type')}  specialty={e['specialty']}")
    print(f"  sections={len(secs)}  plain_text_chars={chars:,}")
    print(f"  preview: {preview}...")
    print()
