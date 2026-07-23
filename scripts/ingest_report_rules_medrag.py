#!/usr/bin/env python3
"""Ingest hospital/insurance report-title rules into MedicalRAG / Qdrant.

Does NOT modify MedicalRAG source. Copies the rules PDF into MedicalRAG's
library directory (via env) and invokes their documented catalog updater /
embed entrypoint via subprocess.

Required env (see repo-root .env.example):
  REPORT_RULES_PDF     — PDF to ingest (default: backend/data/report_rules/*.pdf)
  MEDRAG_ROOT          — MedicalRAG checkout (for PYTHONPATH / scripts)
  MEDRAG_LIBRARY_DIR   — EN library root (specialty subfolders with *.pdf)
  MEDRAG_PYTHON        — optional python that has medrag installed

Usage:
  .venv/Scripts/python.exe scripts/ingest_report_rules_medrag.py
  .venv/Scripts/python.exe scripts/ingest_report_rules_medrag.py --skip-embed
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"


def _load_dotenv() -> None:
    for p in (ROOT / ".env", BACKEND / ".env"):
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def _default_pdf() -> Path:
    raw = (os.getenv("REPORT_RULES_PDF") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    cand = BACKEND / "data" / "report_rules" / "hospital_insurance_report_titles.pdf"
    return cand.resolve()


def _ensure_pdf(pdf: Path) -> Path:
    if pdf.is_file():
        return pdf
    # Build from sibling txt/docx if needed
    txt = pdf.with_suffix(".txt")
    docx = pdf.with_suffix(".docx")
    text = ""
    if txt.is_file():
        text = txt.read_text(encoding="utf-8")
    elif docx.is_file():
        from docx import Document

        doc = Document(str(docx))
        text = "\n".join(
            (p.text or "").rstrip() for p in doc.paragraphs if (p.text or "").strip()
        )
    if not text:
        raise SystemExit(f"No rules PDF/TXT/DOCX found near {pdf}")
    try:
        import fitz
    except ImportError as e:
        raise SystemExit(
            "pymupdf required to build rules PDF — pip install pymupdf"
        ) from e
    pdf.parent.mkdir(parents=True, exist_ok=True)
    docpdf = fitz.open()
    chunk = 3500
    i = 0
    while i < len(text):
        page = docpdf.new_page(width=595, height=842)
        rect = fitz.Rect(40, 40, 555, 800)
        page.insert_textbox(rect, text[i : i + chunk], fontsize=9, fontname="helv")
        i += chunk
    docpdf.save(str(pdf))
    docpdf.close()
    print(f"Wrote {pdf}")
    return pdf


def main() -> None:
    _load_dotenv()
    ap = argparse.ArgumentParser(description="Ingest report-title rules into MedicalRAG")
    ap.add_argument("--pdf", default="", help="Override REPORT_RULES_PDF")
    ap.add_argument(
        "--specialty",
        default=os.getenv("REPORT_RULES_MEDRAG_SPECIALTY", "radiology"),
        help="Library specialty folder name",
    )
    ap.add_argument(
        "--skip-embed",
        action="store_true",
        help="Only copy into library; do not run embed",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without copying / embedding",
    )
    args = ap.parse_args()

    pdf = Path(args.pdf).expanduser().resolve() if args.pdf else _ensure_pdf(_default_pdf())
    if not pdf.is_file():
        raise SystemExit(f"Rules PDF missing: {pdf}")

    library = (os.getenv("MEDRAG_LIBRARY_DIR") or "").strip()
    medrag_root = (os.getenv("MEDRAG_ROOT") or "").strip()
    if not library:
        raise SystemExit(
            "MEDRAG_LIBRARY_DIR is not set. Point it at MedicalRAG's EN library "
            "root (specialty subfolders). See .env.example."
        )
    lib_root = Path(library).expanduser().resolve()
    dest_dir = lib_root / args.specialty.replace(" ", "_")
    dest = dest_dir / pdf.name

    print(f"Source PDF: {pdf}")
    print(f"Dest:       {dest}")
    if args.dry_run:
        print("(dry-run) skip copy/embed")
        return

    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf, dest)
    print(f"Copied rules PDF → {dest}")

    if args.skip_embed:
        print("Skipped embed (--skip-embed). Run MedicalRAG embed later.")
        return

    py = (os.getenv("MEDRAG_PYTHON") or "").strip() or sys.executable
    env = os.environ.copy()
    if medrag_root:
        src = str(Path(medrag_root).expanduser().resolve() / "src")
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        env.setdefault("MEDRAG_ROOT", str(Path(medrag_root).expanduser().resolve()))

    # Sync catalog, then embed ONLY this book (updater.add embeds the whole library).
    cmd_sync = [py, "-m", "medrag.catalog.registry"]
    # registry module may not be a CLI — use a tiny inline sync
    sync_code = (
        "from medrag.catalog.registry import sync_registry; "
        "print(sync_registry())"
    )
    print("Syncing MedicalRAG registry…")
    proc_sync = subprocess.run(
        [py, "-c", sync_code], env=env, cwd=medrag_root or None, check=False
    )
    if proc_sync.returncode != 0:
        print(f"(warn) registry sync exit {proc_sync.returncode} — continuing")

    cmd = [
        py,
        "-m",
        "medrag.index.build_index",
        "--corpus",
        "library",
        "--book",
        pdf.stem,
        "--no-cleanup",
    ]
    print("Running:", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, env=env, cwd=medrag_root or None, check=False)
    except FileNotFoundError as e:
        raise SystemExit(f"Failed to launch MedicalRAG python ({py}): {e}") from e

    if proc.returncode != 0:
        raise SystemExit(
            f"MedicalRAG embed failed (exit {proc.returncode}). "
            "Ensure Qdrant + embed endpoint are up and MEDRAG_ROOT / "
            "MEDRAG_PYTHON / MEDRAG_LIBRARY_DIR are set."
        )
    print("Ingest complete.")


if __name__ == "__main__":
    main()
