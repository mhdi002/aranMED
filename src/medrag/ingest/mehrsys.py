"""Extract text from Mehrsys Medical Library book packs.

Each book is a folder with:
  info.json   — title/id/type
  fts.db      — SQLite FTS with HTML sections (best source)
  content.db  — JSON blobs with HTML
  toc.db      — table of contents
  images.db   — base64 figures (ignored for text RAG)

Not PDFs — ClinicalKey / similar offline packs.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import sqlite3
from pathlib import Path

_TAG_RE = re.compile(r"<[^>]+>", re.S)
_WS_RE = re.compile(r"\s+")

# Title keywords → specialty (EN library-ish tags)
_SPEC_KW: list[tuple[str, tuple[str, ...]]] = [
    ("cardiology", ("cardio", "heart", "braunwald", "electrophysiology", "ecg", "echocardi")),
    ("surgery", ("surg", "trauma", "operative", "sabiston", "schwartz", "vascular")),
    ("pediatrics", ("pediatr", "neonat", "nelson", "child", "infant")),
    ("neurology", ("neuro", "stroke", "epilep", "brain", "spinal")),
    ("psychiatry", ("psychiatr", "mental")),
    ("obgyn", ("obstet", "gynecol", "maternal", "fetal", "williams obst")),
    ("orthopedics", ("orthop", "fracture", "rockwood", "spine", "shoulder")),
    ("dermatology", ("derm", "skin", "andrews")),
    ("ophthalmology", ("ophthal", "eye", "vision")),
    ("ent", ("otolaryng", "head and neck", "ear", "sinus")),
    ("urology", ("urolog", "prostate", "bladder")),
    ("nephrology", ("nephro", "kidney", "dialysis", "brenner")),
    ("gi", ("gastro", "hepat", "liver", "intestinal", "pancrea")),
    ("pulmonology", ("pulmon", "respirat", "lung", "chest")),
    ("hematology__oncology", ("hemat", "oncol", "cancer", "leukem", "lymphoma", "blood")),
    ("infectious_diseases", ("infect", "antimicrobial", "microbiol")),
    ("rheumatology", ("rheum", "arthritis", "lupus")),
    ("anesthesiology", ("anesth", "pain", "perioperative")),
    ("critical_care", ("critical care", "intensive care", "icu", "emergency medicine")),
    ("emergency_medicine", ("emergency", "tintinalli", "rosen")),
    ("radiology", ("radiol", "imaging", "ultrasound", "mri", "nuclear")),
    ("pathology", ("pathol", "histol", "cytopath")),
    ("basic_science", ("anatomy", "physiology", "pharmacol", "embryol", "histology")),
    ("rehabilitation_medicine", ("rehabil", "physical medicine")),
]


def guess_specialty(title: str) -> str:
    n = title.lower()
    for spec, kws in _SPEC_KW:
        if any(k in n for k in kws):
            return spec
    return "general"


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    t = _TAG_RE.sub(" ", t)
    t = html_lib.unescape(t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def is_mehrsys_pack(path: Path) -> bool:
    """True if path is a Mehrsys book folder (or its fts.db / info.json)."""
    p = Path(path)
    if p.is_file():
        p = p.parent
    return (p / "info.json").exists() and (p / "fts.db").exists()


def pack_dir(path: Path) -> Path:
    p = Path(path)
    return p.parent if p.is_file() else p


def read_info(folder: Path) -> dict:
    return json.loads((folder / "info.json").read_text(encoding="utf-8"))


def pack_content_hash(folder: Path) -> str:
    """Stable hash of pack payload (fts.db preferred)."""
    folder = pack_dir(folder)
    target = folder / "fts.db"
    if not target.exists():
        target = folder / "content.db"
    h = hashlib.md5()
    with open(target, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    h.update(folder.name.encode("utf-8"))
    return h.hexdigest()


def extract_sections(folder: Path) -> list[tuple[int, str, str]]:
    """Return list of (section_index, section_title, plain_text)."""
    folder = pack_dir(folder)
    fts = folder / "fts.db"
    out: list[tuple[int, str, str]] = []
    if fts.exists():
        conn = sqlite3.connect(str(fts))
        try:
            rows = conn.execute(
                "SELECT contentId, title, displayTitle, content FROM fts ORDER BY rowid"
            ).fetchall()
        except sqlite3.Error:
            rows = conn.execute(
                "SELECT contentId, title, content FROM fts ORDER BY rowid"
            ).fetchall()
            rows = [(r[0], r[1], r[1], r[2]) for r in rows]
        conn.close()
        for i, (_cid, title, display, content) in enumerate(rows, 1):
            heading = (display or title or f"Section {i}").strip()
            body = html_to_text(content or "")
            if not body:
                continue
            text = f"{heading}\n\n{body}" if heading else body
            out.append((i, heading, text))
        if out:
            return out

    # Fallback: content.db JSON blobs
    cdb = folder / "content.db"
    if not cdb.exists():
        return out
    conn = sqlite3.connect(str(cdb))
    rows = conn.execute("SELECT id, json FROM content ORDER BY rowid").fetchall()
    conn.close()
    for i, (_id, blob) in enumerate(rows, 1):
        try:
            obj = json.loads(blob)
        except Exception:
            continue
        heading = (obj.get("title") or f"Section {i}").strip()
        body = html_to_text(obj.get("html") or "")
        if not body:
            continue
        out.append((i, heading, f"{heading}\n\n{body}"))
    return out


def extract_pages(folder: Path):
    """Yield (page_no, text) treating each FTS section as a 'page'."""
    for idx, _title, text in extract_sections(folder):
        if text.strip():
            yield idx, text


def scan_mehrsys_dir(root: Path | None) -> list[dict]:
    """Scan Mehrsys books root for pack folders."""
    if not root or not Path(root).exists():
        return []
    root = Path(root)
    entries = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not is_mehrsys_pack(d):
            continue
        try:
            info = read_info(d)
        except Exception:
            continue
        title = info.get("title") or d.name.replace("_", " ")
        specialty = guess_specialty(title)
        entries.append({
            "title": title,
            "specialty": specialty,
            "file_path": str(d.resolve()),
            "language": "en",
            "doc_type": "textbook",
            "source_corpus": "mehrsys",
            "status": "downloaded",
            "mehrsys_id": info.get("id"),
            "mehrsys_type": info.get("type"),
            "folder_name": d.name,
        })
    return entries
