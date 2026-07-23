#!/usr/bin/env python3
"""Import institutional report templates (RTF/DOCX) into backend/data/templates/*.txt.

Source folder is passed via --source (never hardcoded Downloads paths).
Existing *.txt files in the templates dir are removed first (folder kept).

Usage:
  .venv/Scripts/python.exe scripts/import_report_templates.py \\
      --source "C:/path/to/dolat khah - Copy (1)"
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = ROOT / "backend" / "data" / "templates"


def _slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower())
    return text.strip("_")[:80] or "template"


def _rtf_to_text(path: Path) -> str:
    from striprtf.striprtf import rtf_to_text

    raw = path.read_bytes()
    for enc in ("utf-8", "cp1256", "latin-1"):
        try:
            s = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        s = raw.decode("latin-1", errors="replace")
    return rtf_to_text(s)


def _docx_to_text(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts: list[str] = []
    for p in doc.paragraphs:
        t = (p.text or "").rstrip()
        if t.strip():
            parts.append(t)
    return "\n".join(parts)


def _is_clean_title(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) < 3 or len(t) > 100:
        return False
    if any(ord(c) < 32 for c in t):
        return False
    # Reject OLE / binary leftovers
    if "bjbj" in t or "\x00" in t:
        return False
    printable = sum(1 for c in t if c.isprintable() or c.isspace())
    if printable / max(1, len(t)) < 0.95:
        return False
    return True


def _clean_body(text: str) -> str:
    # Drop binary/control-heavy lines; normalise newlines.
    lines_out: list[str] = []
    for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        # Strip NULs and most C0 controls
        cleaned = "".join(ch if (ch.isprintable() or ch in "\t") else " " for ch in ln)
        cleaned = cleaned.rstrip()
        if "bjbj" in cleaned.lower():
            continue
        # Skip lines that are mostly non-letter garbage
        letters = sum(1 for c in cleaned if c.isalpha())
        if cleaned and letters < max(3, int(0.2 * len(cleaned))) and len(cleaned) > 40:
            continue
        lines_out.append(cleaned)
    while lines_out and not lines_out[0].strip():
        lines_out.pop(0)
    while lines_out and not lines_out[-1].strip():
        lines_out.pop()
    return "\n".join(lines_out).strip() + "\n"


def _display_title(stem: str, body: str) -> str:
    """Prefer a clean body heading that matches the filename; else the stem.

    Filename is authoritative for the picker when the RTF body starts with an
    unrelated exam banner or binary junk (common in legacy Word exports).
    """
    stem_title = " ".join(stem.split()).strip() or "template"
    stem_tokens = {t for t in re.findall(r"[a-z0-9]+", stem_title.lower()) if len(t) > 2}
    for ln in body.splitlines():
        t = ln.strip()
        if not t:
            continue
        if t.endswith(":") and len(t) > 3:
            t = t[:-1].strip()
        if not _is_clean_title(t):
            continue
        body_tokens = {x for x in re.findall(r"[a-z0-9]+", t.lower()) if len(x) > 2}
        if stem_tokens & body_tokens:
            return t
        # No filename overlap — only accept body title when stem has no tokens
        if not stem_tokens and len(t.split()) <= 12:
            return t
    return stem_title


def _collect_sources(src: Path) -> list[tuple[Path, str]]:
    """Return (file, relative_prefix) for every RTF/DOCX under src."""
    out: list[tuple[Path, str]] = []
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".rtf", ".docx"):
            continue
        # Skip Word lock / temp files
        if p.name.startswith("~$"):
            continue
        rel = p.relative_to(src)
        # If nested under one wrapper folder (e.g. "dolat khah - Copy/…"), use
        # subdirectory names as id prefixes (e.g. CDS/).
        parts = list(rel.parts[:-1])
        # Drop a single top-level wrapper that looks like the export folder name.
        if parts and ("dolat" in parts[0].lower() or "copy" in parts[0].lower()):
            parts = parts[1:]
        prefix = "_".join(_slugify(x) for x in parts if x) if parts else ""
        out.append((p, prefix))
    return out


def import_templates(source: Path, dest: Path, *, clear: bool = True) -> dict:
    source = source.resolve()
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)

    removed = 0
    if clear:
        for old in dest.glob("*.txt"):
            old.unlink()
            removed += 1

    files = _collect_sources(source)
    if not files:
        raise SystemExit(f"No .rtf/.docx files found under {source}")

    saved = 0
    used_ids: set[str] = set()
    rows: list[dict] = []

    for path, prefix in files:
        stem = path.stem.strip()
        if path.suffix.lower() == ".rtf":
            body = _clean_body(_rtf_to_text(path))
        else:
            body = _clean_body(_docx_to_text(path))
        title = _display_title(stem, body)
        base_id = _slugify(f"{prefix}_{stem}" if prefix else stem)
        tid = base_id
        n = 2
        while tid in used_ids:
            tid = f"{base_id}_{n}"
            n += 1
        used_ids.add(tid)

        # Header preserves insurance-critical original title/name.
        header = (
            f"# template_id: {tid}\n"
            f"# title: {title}\n"
            f"# source_name: {stem}\n"
            f"# source_file: {path.name}\n"
            "\n"
        )
        out_path = dest / f"{tid}.txt"
        out_path.write_text(header + body, encoding="utf-8")
        saved += 1
        rows.append({"id": tid, "title": title, "source": path.name, "chars": len(body)})
        print(f"  {tid:50s} <- {path.relative_to(source)}")

    return {"removed": removed, "saved": saved, "templates": rows, "dest": str(dest)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Import RTF/DOCX report templates → *.txt")
    ap.add_argument(
        "--source",
        required=True,
        help="Folder containing institutional templates (RTF/DOCX), read-only source",
    )
    ap.add_argument(
        "--dest",
        default=str(DEFAULT_DEST),
        help="Destination templates dir (default: backend/data/templates)",
    )
    ap.add_argument(
        "--keep-existing",
        action="store_true",
        help="Do not delete existing *.txt before import",
    )
    args = ap.parse_args()
    src = Path(args.source)
    if not src.is_dir():
        raise SystemExit(f"Source not a directory: {src}")

    print(f"Importing from {src}")
    print(f"Into {args.dest}")
    stats = import_templates(src, Path(args.dest), clear=not args.keep_existing)
    print(f"Removed {stats['removed']} old .txt; saved {stats['saved']} templates.")


if __name__ == "__main__":
    main()
