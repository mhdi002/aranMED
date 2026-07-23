"""Disk cache for bge-m3 ColBERT token vectors (late-interaction reranking)."""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import numpy as np

from medrag.config import DATA_DIR

_CACHE_DIR = DATA_DIR / "colbert_cache"
_lock = threading.Lock()


def _key(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()


def _path(text: str) -> Path:
    return _CACHE_DIR / f"{_key(text)}.npz"


def get(text: str) -> np.ndarray | None:
    p = _path(text)
    if not p.exists():
        return None
    try:
        return np.load(p)["vecs"]
    except Exception:
        return None


def put(text: str, vecs: np.ndarray):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        np.savez_compressed(_path(text), vecs=vecs)


def get_batch(texts: list[str]) -> tuple[list[np.ndarray | None], list[int]]:
    """Return cached vecs and indices that need computation."""
    cached: list[np.ndarray | None] = []
    missing_idx: list[int] = []
    for i, t in enumerate(texts):
        v = get(t)
        cached.append(v)
        if v is None:
            missing_idx.append(i)
    return cached, missing_idx
