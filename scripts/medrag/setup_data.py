"""Bootstrap data from source projects (indexes, extracted text, OCR output)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEDICAL_RAG = ROOT.parent / "medical-rag"
MEDRAG = ROOT.parent.parent / "Data-main" / "medrag"


def _copy_file(src: Path, dst: Path):
    if not src.exists():
        print(f"[skip] {src} not found")
        return
    if dst.exists():
        print(f"[skip] {dst} already exists")
        return
    shutil.copy2(src, dst)
    print(f"[copy] {src} -> {dst}")


def _copy_tree(src: Path, dst: Path, pattern="*"):
    if not src.exists():
        print(f"[skip] {src} not found")
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in src.glob(pattern):
        if f.is_file():
            target = dst / f.name
            if not target.exists():
                shutil.copy2(f, target)
                n += 1
    print(f"[copy] {src} -> {dst}: {n} files")
    return n


def _copy_dir(src: Path, dst: Path):
    if not src.exists():
        print(f"[skip] {src} not found")
        return
    if dst.exists() and any(dst.iterdir()):
        print(f"[skip] {dst} already populated")
        return
    shutil.copytree(src, dst, dirs_exist_ok=True)
    print(f"[copytree] {src} -> {dst}")


def generate_ocr_samples():
    """Render test pages from OCR'd books and build manifest."""
    import fitz
    ocr_dir = MEDICAL_RAG / "data" / "ocr_markdown"
    samples_dir = ROOT / "eval" / "ocr_samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    if not ocr_dir.exists():
        return
    for jsonl in list(ocr_dir.glob("*.jsonl"))[:3]:
        rows = [json.loads(l) for l in open(jsonl, encoding="utf-8")]
        if not rows:
            continue
        page_row = rows[min(5, len(rows) - 1)]
        book_name = page_row.get("book", "")
        manifest_path = ROOT / "data" / "manifest.jsonl"
        pdf_path = None
        if manifest_path.exists():
            for line in open(manifest_path, encoding="utf-8"):
                m = json.loads(line)
                if m.get("file") == book_name:
                    pdf_path = Path(m["path"])
                    break
        if not pdf_path or not pdf_path.exists():
            continue
        page_num = page_row["page"] - 1
        doc = fitz.open(pdf_path)
        if page_num >= doc.page_count:
            doc.close()
            continue
        img_name = f"{jsonl.stem}_p{page_row['page']}.png"
        pix = doc[page_num].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        pix.save(str(samples_dir / img_name))
        doc.close()
        words = [w for w in page_row["text"].split() if len(w) > 4][:8]
        manifest.append({
            "image": img_name,
            "book": book_name,
            "page": page_row["page"],
            "must_any": words[:5] if len(words) >= 3 else words,
            "min_recall": 0.5,
        })
    if manifest:
        out = samples_dir / "manifest.jsonl"
        with open(out, "w", encoding="utf-8") as f:
            for m in manifest:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        print(f"[ocr_samples] wrote {len(manifest)} samples -> {out}")


def main():
    _copy_file(MEDRAG / "catalog.db", ROOT / "catalog.db")
    _copy_dir(MEDRAG / "qdrant_storage", ROOT / "qdrant_storage")
    _copy_tree(MEDICAL_RAG / "data" / "text", ROOT / "data" / "text")
    _copy_tree(MEDICAL_RAG / "data" / "ocr_markdown", ROOT / "data" / "ocr_markdown")
    if (MEDICAL_RAG / "data" / "manifest.jsonl").exists():
        shutil.copy2(MEDICAL_RAG / "data" / "manifest.jsonl", ROOT / "data" / "manifest.jsonl")
    generate_ocr_samples()
    print("Setup complete.")


if __name__ == "__main__":
    main()
