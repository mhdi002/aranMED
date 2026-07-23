"""Unpack and catalog Iranian standards/rules archives (zip/rar → text PDFs)."""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from medrag.config import STANDARDS_DIR, STANDARDS_EXTRACTED

UNRAR = Path(r"C:\Program Files\WinRAR\UnRAR.exe")
ARCHIVE_EXTS = {".zip", ".rar", ".7z"}

_LEGAL_KW = ("قانون", "آیین", "آئين", "مقرره", "مصوبه", "legal", "law", "regulation")
_STD_KW = ("استاندارد", "sop", "standard", "protocol", "پروتکل")
_GUIDE_KW = ("راهنما", "guideline", "گایدلاین", "دستورالعمل")


def guess_doc_type(name: str) -> str:
    n = name.lower()
    if any(k in n for k in _LEGAL_KW):
        return "legal"
    if any(k in n for k in _GUIDE_KW):
        return "guideline"
    if any(k in n for k in _STD_KW):
        return "standard"
    return "standard"


def guess_specialty(name: str) -> str:
    n = name.lower()
    mapping = [
        ("cardiology", ("قلب", "cardio", "mpi", "chf", "nst")),
        ("neurology", ("مغز", "اعصاب", "eeg", "ms", "sma", "dms", "ataxia")),
        ("hematology__oncology", ("خون", "thalassemia", "haemophil", "sickle", "thyroid cancer", "imrt")),
        ("genetics", ("ژنتیک", "nipt", "ngs", "mlpa", "cgh", "qpcr", "dbs", "pku", "sma", "dmd")),
        ("pulmonology", ("ریه", "ebus", "tblb", "tbna")),
        ("gi", ("گوارش", "ibd")),
        ("urology", ("اورولوژی", "cystoscopy", "urethrotomy", "varicocele", "hydrocele")),
        ("obgyn", ("زنان", "sacrocolpopexy", "nipt", "nt_")),
        ("radiology", ("رادیو", "oct", "mpi", "iodine")),
        ("psychiatry", ("روان", "rtms", "mslt", "mwt")),
        ("endocrinology", ("غدد", "thyroid", "hyperthyrod", "hmg")),
        ("nephrology", ("کلیه", "tpe")),
    ]
    for spec, kws in mapping:
        if any(k in n for k in kws):
            return spec
    return "standards"


def _safe_stem(name: str) -> str:
    s = re.sub(r"[^\w\s\u0600-\u06FF.-]", "_", name).strip().replace(" ", "_")
    return (s or "archive")[:100]


def _has_text_layer(pdf: Path, sample_pages: int = 5) -> bool:
    try:
        import fitz
        doc = fitz.open(pdf)
    except Exception:
        return False
    try:
        n = min(sample_pages, doc.page_count)
        best = 0
        for i in range(n):
            best = max(best, len(doc[i].get_text("text").strip()))
        return best > 80
    finally:
        doc.close()


def unpack_archive(archive: Path, dest_root: Path | None = None) -> Path:
    dest_root = dest_root or STANDARDS_EXTRACTED
    dest = dest_root / _safe_stem(archive.stem)
    dest.mkdir(parents=True, exist_ok=True)
    marker = dest / ".extracted_ok"
    if marker.exists() and any(dest.rglob("*.pdf")):
        return dest

    ext = archive.suffix.lower()
    if ext == ".zip":
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(dest)
    elif ext in (".rar", ".7z"):
        if not UNRAR.exists():
            raise FileNotFoundError(f"UnRAR not found at {UNRAR}")
        # x = extract with paths; -y = yes; -o+ = overwrite
        cmd = [str(UNRAR), "x", "-y", "-o+", str(archive), str(dest) + "\\"]
        subprocess.run(cmd, check=True, capture_output=True)
    else:
        raise ValueError(f"Unsupported archive: {archive}")
    marker.write_text(archive.name, encoding="utf-8")
    return dest


def _safe_print(msg: str):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


def unpack_all(standards_dir: Path | None = None,
               dest_root: Path | None = None,
               limit: int = 0) -> dict:
    standards_dir = Path(standards_dir or STANDARDS_DIR)
    dest_root = Path(dest_root or STANDARDS_EXTRACTED)
    dest_root.mkdir(parents=True, exist_ok=True)
    stats = {"ok": 0, "skip": 0, "fail": 0, "copied_pdf": 0}
    archives = sorted(
        p for p in standards_dir.iterdir()
        if p.is_file() and p.suffix.lower() in ARCHIVE_EXTS
    )
    if limit:
        archives = archives[:limit]
    for arc in archives:
        try:
            unpack_archive(arc, dest_root)
            stats["ok"] += 1
            _safe_print(f"[ok] unpack {arc.name[:60]}")
        except Exception as e:
            stats["fail"] += 1
            _safe_print(f"[fail] unpack {arc.name[:50]} -- {e}")

    # Loose PDFs at top level
    for pdf in standards_dir.glob("*.pdf"):
        dest = dest_root / "_loose" / pdf.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(pdf, dest)
            stats["copied_pdf"] += 1
    return stats


def scan_standards(extracted_root: Path | None = None,
                   require_text: bool = True) -> list[dict]:
    root = Path(extracted_root or STANDARDS_EXTRACTED)
    if not root.exists():
        return []
    entries = []
    seen = set()
    for pdf in sorted(root.rglob("*.pdf")):
        try:
            key = str(pdf.resolve())
        except Exception:
            key = str(pdf)
        if key in seen:
            continue
        seen.add(key)
        if require_text and not _has_text_layer(pdf):
            continue
        title = pdf.stem
        parent = pdf.parent.name
        if parent not in (".", "_loose", root.name):
            # Prefer archive folder name for specialty/topic
            topic_src = parent + " " + title
        else:
            topic_src = title
        entries.append({
            "title": title,
            "specialty": guess_specialty(topic_src),
            "file_path": str(pdf.resolve()),
            "language": "fa",
            "doc_type": guess_doc_type(topic_src),
            "source_corpus": "standards",
            "status": "downloaded",
            "country": "IR",
            "index_tags": ["legal", "standard", "guideline"][
                :1 if guess_doc_type(topic_src) == "legal" else 2
            ],
            "folder_name": parent,
        })
        # Fix index_tags properly
        dt = entries[-1]["doc_type"]
        tags = [dt]
        if dt == "legal":
            tags.append("legal")
        else:
            tags.append("guideline")
        entries[-1]["index_tags"] = list(dict.fromkeys(tags))
    return entries


def content_hash_path(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()
