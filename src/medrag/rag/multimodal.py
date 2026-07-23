"""Multimodal helpers: Chandra OCR, VLM clinical images, question splitting."""
from __future__ import annotations

import base64
import re

import requests

from medrag.config import OCR_MODEL, OCR_PROMPT, OLLAMA_URL, VISION_MODEL
from medrag.ingest.html_to_text import html_to_text

_Q_START = re.compile(
    r"(?m)^\s*(?:سوال|سؤال|پرسش|question|q)?\s*[\-\.\)]?\s*([۰-۹0-9]{1,3})\s*[\-\.\)ـ:]\s"
)
_IS_QUESTION = re.compile(r"[؟?]|(?:^|\s)(?:الف|ب|ج|د)\s*[\)\(]|(?:^|\s)[A-Da-d]\s*[\)\.]")


def split_questions(text: str) -> list[str]:
    starts = [m.start() for m in _Q_START.finditer(text)]
    if len(starts) < 2:
        t = text.strip()
        return [t] if len(t) > 15 else []
    starts.append(len(text))
    segs = [text[a:b].strip() for a, b in zip(starts, starts[1:]) if len(text[a:b].strip()) > 15]
    out = []
    for seg in segs:
        if _IS_QUESTION.search(seg) or not out:
            out.append(seg)
        else:
            out[-1] += "\n" + seg
    qs = [s for s in out if _IS_QUESTION.search(s)]
    return qs or out


def _chat(model, content, images=None, num_ctx=8192, num_predict=2048, temp=0.2):
    msg = {"role": "user", "content": content}
    if images:
        msg["images"] = images
    r = requests.post(f"{OLLAMA_URL.rstrip('/')}/api/chat", json={
        "model": model, "messages": [msg], "stream": False, "think": False,
        "options": {"temperature": temp, "num_ctx": num_ctx, "num_predict": num_predict},
    }, timeout=600)
    m = r.json().get("message", {})
    out = (m.get("content") or "").strip()
    if not out and m.get("thinking"):
        out = m["thinking"].strip()
    return out


def ocr_image(image_path: str) -> str:
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    r = requests.post(f"{OLLAMA_URL.rstrip('/')}/api/chat", json={
        "model": OCR_MODEL, "think": False,
        "messages": [{"role": "user", "content": OCR_PROMPT, "images": [b64]}],
        "stream": False,
        "options": {"temperature": 0.0, "num_ctx": 8192, "num_predict": 6000},
    }, timeout=600)
    html = (r.json().get("message", {}) or {}).get("content", "") or ""
    return html_to_text(html)


def describe_clinical_image(image_path: str) -> str:
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    prompt = (
        "Describe this medical image (ECG, X-ray, CT/MRI, pathology, clinical photo) "
        "in detail: modality, key findings, and likely diagnosis or differential."
    )
    return _chat(VISION_MODEL, prompt, images=[b64], num_predict=1024)
