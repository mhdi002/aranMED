"""Chandra OCR for scanned Persian/English books via Ollama — optimized pipeline."""
from __future__ import annotations

import argparse
import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fitz
import requests

from medrag.config import (
    MANIFEST, OCR_DPI, OCR_JPEG_QUALITY, OCR_KEEP_ALIVE, OCR_MODEL,
    OCR_NUM_CTX, OCR_NUM_PREDICT, OCR_OUT, OCR_PREFETCH, OCR_PROMPT,
    OCR_SKIP_BLANK, OLLAMA_URL,
)
from medrag.ingest.html_to_text import html_to_text

_executor = ThreadPoolExecutor(max_workers=1)


def _render_page(page, dpi=OCR_DPI) -> bytes:
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    if OCR_JPEG_QUALITY and OCR_JPEG_QUALITY > 0:
        return pix.tobytes("jpeg", jpg_quality=OCR_JPEG_QUALITY)
    return pix.tobytes("png")


def _is_blank(page, dpi=OCR_DPI, threshold=248) -> bool:
    """Skip near-white pages (no content loss on real exam pages)."""
    if not OCR_SKIP_BLANK:
        return False
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix = page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)
    samples = pix.samples
    if not samples:
        return True
    # Sample every 64th byte for speed
    step = max(1, len(samples) // 2000)
    dark = sum(1 for i in range(0, len(samples), step) if samples[i] < threshold)
    return dark < max(3, len(samples) // step // 500)


def ocr_image(image_bytes: bytes, retries=3) -> str:
    b64 = base64.b64encode(image_bytes).decode()
    endpoint = OLLAMA_URL.rstrip("/") + "/api/chat"
    payload = {
        "model": OCR_MODEL,
        "think": False,
        "keep_alive": OCR_KEEP_ALIVE,
        "messages": [{"role": "user", "content": OCR_PROMPT, "images": [b64]}],
        "stream": False,
        "options": {
            "temperature": 0.0,
            "num_ctx": OCR_NUM_CTX,
            "num_predict": OCR_NUM_PREDICT,
        },
    }
    for attempt in range(retries):
        try:
            r = requests.post(endpoint, json=payload, timeout=600)
            r.raise_for_status()
            html = (r.json().get("message", {}) or {}).get("content", "") or ""
            return html_to_text(html)
        except Exception as e:
            if attempt == retries - 1:
                return f"[OCR_ERROR: {e}]"
            time.sleep(2 * (attempt + 1))
    return ""


def _warm_model():
    """Pre-load Chandra into VRAM."""
    try:
        requests.post(
            OLLAMA_URL.rstrip("/") + "/api/chat",
            json={
                "model": OCR_MODEL,
                "keep_alive": OCR_KEEP_ALIVE,
                "messages": [{"role": "user", "content": "ok"}],
                "stream": False,
                "options": {"num_predict": 1},
            },
            timeout=120,
        )
    except Exception:
        pass


def ocr_book(meta: dict):
    OCR_OUT.mkdir(parents=True, exist_ok=True)
    stem = Path(meta["file"]).stem
    out = OCR_OUT / f"{stem}.jsonl"
    done = set()
    if out.exists():
        for line in open(out, encoding="utf-8"):
            try:
                done.add(json.loads(line)["page"])
            except Exception:
                pass

    doc = fitz.open(meta["path"])
    n = doc.page_count
    _warm_model()

    pending_render = None
    if OCR_PREFETCH:
        for i in range(n):
            if i + 1 not in done:
                pending_render = _executor.submit(_render_page, doc[i])
                break

    with open(out, "a", encoding="utf-8") as f:
        for i in range(n):
            page = i + 1
            if page in done:
                continue

            t0 = time.time()
            if _is_blank(doc[i]):
                text = ""
            elif OCR_PREFETCH and pending_render is not None:
                img_bytes = pending_render.result()
                # Prefetch next page while GPU runs
                pending_render = None
                for j in range(i + 1, n):
                    if j + 1 not in done:
                        pending_render = _executor.submit(_render_page, doc[j])
                        break
                text = ocr_image(img_bytes)
            else:
                text = ocr_image(_render_page(doc[i]))

            f.write(json.dumps({
                "book": meta["file"],
                "language": meta.get("language", "fa"),
                "specialty": meta.get("specialty", "general"),
                "type": meta.get("type", "qbank"),
                "page": page,
                "text": text,
            }, ensure_ascii=False) + "\n")
            f.flush()

            if page % 5 == 0 or page == 1:
                print(f"    {stem[:26]:26s} p{page}/{n} ({time.time()-t0:.0f}s)", flush=True)

    doc.close()


def ocr_all(manifest_rows: list[dict], book_name: str | None = None):
    todo = [
        r for r in manifest_rows
        if r.get("needs_ocr") and not r.get("duplicate") and not r.get("excluded")
    ]
    if book_name:
        todo = [r for r in todo if r["file"] == book_name]
    print(f"OCR (Chandra, optimized): {len(todo)} book(s)", flush=True)
    for r in todo:
        ocr_book(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default=None)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    rows = [json.loads(l) for l in open(MANIFEST, encoding="utf-8")]
    if not args.all and not args.book:
        raise SystemExit("Pass --book <name> or --all")
    ocr_all(rows, args.book)


if __name__ == "__main__":
    main()
