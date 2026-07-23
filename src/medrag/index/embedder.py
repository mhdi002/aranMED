"""Shared embedding + reranking (bge-m3 dense+sparse+ColBERT, bge-reranker-v2-m3).

Providers (config/env — no hardcoded URLs or model ids):
  - local: FlagEmbedding BGEM3FlagModel (EMBED_MODEL)
  - vllm | openai: OpenAI-compatible POST {EMBED_BASE_URL}/embeddings
"""
from __future__ import annotations

import logging
import threading

import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from medrag.config import (
    COLBERT_ENABLED,
    EMBED_API_KEY,
    EMBED_BASE_URL,
    EMBED_BATCH,
    EMBED_MAX_LENGTH,
    EMBED_MODEL,
    EMBED_PROVIDER,
    EMBED_TIMEOUT,
    RERANK_DEVICE,
    RERANK_MODEL,
    SPARSE_EMBED,
)
from medrag.index import colbert_cache
from medrag.index._compat import patch_bge_m3_sparse, patch_torch_load_check

log = logging.getLogger(__name__)

_lock = threading.Lock()
_embed = None
_rerank = None
_http: requests.Session | None = None

# bge-m3 XLM-R vocab — clamp sparse token ids below this
_MAX_SPARSE_TOKEN_ID = 169511


def _openai_embed_base(url: str) -> str:
    """Normalize to .../v1 for embeddings (same rules as chat base)."""
    u = (url or "").rstrip("/")
    if not u:
        raise ValueError("EMBED_BASE_URL is empty")
    if u.endswith("/v1"):
        return u
    if u.endswith("/embeddings"):
        return u[: -len("/embeddings")]
    if u.count("/") <= 2 or (":" in u.split("//")[-1] and u.count("/") <= 3):
        return u + "/v1"
    return u


def _http_session() -> requests.Session:
    global _http
    if _http is None:
        s = requests.Session()
        retry = Retry(total=2, backoff_factor=0.3, status_forcelist=(502, 503, 504))
        s.mount("http://", HTTPAdapter(max_retries=retry))
        s.mount("https://", HTTPAdapter(max_retries=retry))
        _http = s
    return _http


def get_embedder():
    """Local FlagEmbedding model (lazy). Not used when EMBED_PROVIDER is vllm|openai."""
    global _embed
    if _embed is None:
        with _lock:
            if _embed is None:
                patch_torch_load_check()
                patch_bge_m3_sparse()
                import torch
                from FlagEmbedding import BGEM3FlagModel
                device = "cuda:0" if torch.cuda.is_available() else "cpu"
                _embed = BGEM3FlagModel(EMBED_MODEL, use_fp16=True, devices=device)
    return _embed


def get_reranker():
    global _rerank
    if _rerank is None:
        with _lock:
            if _rerank is None:
                patch_torch_load_check()
                import torch
                from transformers import AutoTokenizer, AutoModelForSequenceClassification
                tok = AutoTokenizer.from_pretrained(RERANK_MODEL, use_fast=True)
                mdl = AutoModelForSequenceClassification.from_pretrained(RERANK_MODEL)
                if RERANK_DEVICE:
                    dev = RERANK_DEVICE
                elif EMBED_PROVIDER in ("vllm", "openai"):
                    # Leave GPU for vLLM LLM/embed servers
                    dev = "cpu"
                else:
                    dev = "cuda" if torch.cuda.is_available() else "cpu"
                mdl = mdl.to(dev).eval()
                if dev.startswith("cuda"):
                    mdl = mdl.half()
                _rerank = (tok, mdl, dev)
    return _rerank


def _sanitize_sparse(sp: dict) -> dict:
    out = {}
    for k, v in sp.items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        if 0 <= idx <= _MAX_SPARSE_TOKEN_ID and float(v) > 0:
            out[idx] = float(v)
    return out


def _truncate_texts(texts: list[str], max_chars: int = 1200) -> list[str]:
    return [t[:max_chars] if len(t) > max_chars else t for t in texts]


def _encode_once(model, texts: list[str], batch_size: int, max_length: int, sparse: bool):
    return model.encode(
        texts, batch_size=batch_size, max_length=max_length,
        return_dense=True, return_sparse=sparse,
        return_colbert_vecs=False,
    )


def _pack_encode_out(out, n: int) -> list[dict]:
    dense = out["dense_vecs"]
    sparse = out.get("lexical_weights")
    results = []
    for i in range(n):
        if sparse is None or not sparse:
            sp = {}
        else:
            sp = {int(k): float(v) for k, v in sparse[i].items()}
        results.append({"dense": dense[i].tolist(), "sparse": _sanitize_sparse(sp)})
    return results


def _encode_batch_with_fallback(model, texts: list[str], batch_size: int,
                                max_length: int, use_sparse: bool) -> list[dict]:
    """Encode a list of texts, shrinking batch/length on CUDA OOM."""
    n = len(texts)
    if n == 0:
        return []

    try:
        out = _encode_once(model, texts, min(batch_size, n), max_length, use_sparse)
        return _pack_encode_out(out, n)
    except Exception:
        _reset_embedder()
        model = get_embedder()

    if n > 1:
        mid = max(1, n // 2)
        left = _encode_batch_with_fallback(model, texts[:mid], mid, max_length, use_sparse)
        model = get_embedder()
        right = _encode_batch_with_fallback(model, texts[mid:], mid, max_length, use_sparse)
        return left + right

    t = texts[0]
    for sparse_try, ml, mc in (
        (use_sparse, max_length, 1200),
        (use_sparse, min(max_length, 256), 800),
        (False, min(max_length, 256), 600),
    ):
        try:
            out = _encode_once(model, [t[:mc]], 1, ml, sparse_try)
            return _pack_encode_out(out, 1)
        except Exception:
            _reset_embedder()
            model = get_embedder()
    raise RuntimeError(f"encode failed for text len={len(t)}")


def _encode_openai_compat(texts: list[str], batch_size: int) -> list[dict]:
    """Dense-only via OpenAI-compatible /v1/embeddings (vLLM embed server)."""
    base = _openai_embed_base(EMBED_BASE_URL)
    url = f"{base}/embeddings"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {EMBED_API_KEY or 'EMPTY'}",
    }
    results: list[dict] = []
    session = _http_session()
    for start in range(0, len(texts), batch_size):
        chunk = texts[start:start + batch_size]
        payload = {"model": EMBED_MODEL, "input": chunk}
        try:
            r = session.post(url, json=payload, headers=headers, timeout=EMBED_TIMEOUT)
            r.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(
                f"Embeddings endpoint unreachable at {url} "
                f"(provider={EMBED_PROVIDER}, model={EMBED_MODEL}). "
                f"Set MEDRAG_EMBED_BASE_URL / VLLM_EMBED_BASE_URL. "
                f"Underlying: {type(e).__name__}: {e}"
            ) from e
        data = r.json()
        items = sorted(data.get("data") or [], key=lambda x: int(x.get("index", 0)))
        if len(items) != len(chunk):
            raise RuntimeError(
                f"embeddings response length mismatch: got {len(items)} expected {len(chunk)}"
            )
        for item in items:
            vec = item.get("embedding")
            if not vec:
                raise RuntimeError("embeddings response missing embedding vector")
            results.append({"dense": list(vec), "sparse": {}})
    return results


def encode(texts, batch_size=None, is_query=False, max_length=None):
    batch_size = max(1, batch_size or EMBED_BATCH)
    texts = _truncate_texts(texts)
    if is_query:
        texts = [f"Represent this sentence for searching relevant passages: {t}" for t in texts]
    else:
        texts = [f"Represent this document for retrieval: {t}" for t in texts]

    if EMBED_PROVIDER in ("vllm", "openai"):
        return _encode_openai_compat(texts, batch_size)

    model = get_embedder()
    max_length = max_length or EMBED_MAX_LENGTH
    results = []
    use_sparse = SPARSE_EMBED
    for start in range(0, len(texts), batch_size):
        chunk = texts[start:start + batch_size]
        results.extend(
            _encode_batch_with_fallback(model, chunk, batch_size, max_length, use_sparse)
        )
        model = get_embedder()
    return results


def _reset_embedder():
    global _embed
    _embed = None
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def release_local_models(*, keep_cpu_reranker: bool = True) -> dict:
    """Drop local FlagEmbedding (+ optional CUDA reranker) to free VRAM.

    Used before LLM generation on 8GB cards. CPU reranker is kept by default
    so compress/rerank does not reload weights every question.
    """
    global _embed, _rerank
    notes: dict = {"embedder": False, "reranker": False}
    with _lock:
        if _embed is not None:
            _embed = None
            notes["embedder"] = True
        if _rerank is not None and not keep_cpu_reranker:
            _rerank = None
            notes["reranker"] = True
        elif _rerank is not None and keep_cpu_reranker:
            # Drop only if reranker was placed on CUDA
            try:
                _tok, _mdl, dev = _rerank
                if isinstance(dev, str) and dev.startswith("cuda"):
                    _rerank = None
                    notes["reranker"] = True
                else:
                    notes["reranker_kept"] = f"cpu:{dev}"
            except Exception:
                notes["reranker_kept"] = "unknown"
    try:
        import gc
        gc.collect()
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            notes["cuda_empty_cache"] = True
    except Exception as e:
        notes["error"] = str(e)
    if notes["embedder"] or notes["reranker"]:
        log.info("Released local embed/rerank models for LLM headroom: %s", notes)
    return notes


def encode_queries(texts, batch_size=None):
    return encode(texts, batch_size=batch_size or EMBED_BATCH, is_query=True)


def encode_passages(texts, batch_size=None):
    return encode(texts, batch_size=batch_size or EMBED_BATCH, is_query=False)


def embed_health() -> dict:
    """Probe embeddings backend readiness."""
    provider = EMBED_PROVIDER
    if provider in ("vllm", "openai"):
        try:
            base = _openai_embed_base(EMBED_BASE_URL)
            r = _http_session().get(f"{base}/models", timeout=5)
            return {
                "provider": provider,
                "ok": r.status_code < 500,
                "url": base,
                "model": EMBED_MODEL,
                "status": r.status_code,
            }
        except requests.RequestException as e:
            return {
                "provider": provider,
                "ok": False,
                "url": EMBED_BASE_URL,
                "model": EMBED_MODEL,
                "error": str(e),
            }
    return {"provider": "local", "ok": True, "model": EMBED_MODEL}


def _encode_colbert_raw(texts: list[str], is_query: bool, batch_size: int = 16):
    if EMBED_PROVIDER in ("vllm", "openai"):
        raise RuntimeError(
            "ColBERT vectors require local FlagEmbedding; "
            "disable retrieval.colbert_enabled when using vLLM embeddings"
        )
    model = get_embedder()
    if is_query:
        texts = [f"Represent this sentence for searching relevant passages: {t}" for t in texts]
    else:
        texts = [f"Represent this document for retrieval: {t}" for t in texts]
    out = model.encode(
        texts, batch_size=min(batch_size, 16), max_length=512,
        return_dense=False, return_sparse=False, return_colbert_vecs=True,
    )
    return out["colbert_vecs"]


def _to_numpy(vec) -> np.ndarray:
    if isinstance(vec, np.ndarray):
        return vec
    try:
        import torch
        if isinstance(vec, torch.Tensor):
            return vec.detach().cpu().numpy()
    except Exception:
        pass
    return np.asarray(vec)


def colbert_rerank(query: str, passages: list[str], batch_size: int = 8) -> list[float]:
    """Late-interaction MaxSim scores via bge-m3 ColBERT vectors."""
    if not COLBERT_ENABLED or not passages:
        return [0.0] * len(passages)
    # vLLM OpenAI embed API has no ColBERT — skip gracefully
    if EMBED_PROVIDER in ("vllm", "openai"):
        return [0.0] * len(passages)

    model = get_embedder()
    q_vecs = _encode_colbert_raw([query], is_query=True, batch_size=1)
    q_vec = _to_numpy(q_vecs[0])

    cached, missing = colbert_cache.get_batch(passages)
    p_vecs: list = [None] * len(passages)

    for i, v in enumerate(cached):
        if v is not None:
            p_vecs[i] = v

    if missing:
        to_encode = [passages[i] for i in missing]
        for start in range(0, len(to_encode), batch_size):
            batch = to_encode[start:start + batch_size]
            batch_idx = missing[start:start + batch_size]
            encoded = _encode_colbert_raw(batch, is_query=False, batch_size=batch_size)
            for j, vec in enumerate(encoded):
                arr = _to_numpy(vec)
                idx = batch_idx[j]
                p_vecs[idx] = arr
                colbert_cache.put(passages[idx], arr)

    valid_pairs = [(i, p_vecs[i]) for i in range(len(passages)) if p_vecs[i] is not None]
    if not valid_pairs:
        return [0.0] * len(passages)

    q_arr = np.asarray(q_vec, dtype=np.float32)
    out = [0.0] * len(passages)
    for i, p_vec in valid_pairs:
        try:
            p_arr = np.asarray(p_vec, dtype=np.float32)
            scores_raw = model.colbert_score(q_arr, p_arr)
            if hasattr(scores_raw, "detach"):
                out[i] = float(scores_raw.detach().cpu().reshape(-1)[0])
            else:
                out[i] = float(np.asarray(scores_raw).reshape(-1)[0])
        except Exception:
            try:
                p_arr = np.asarray(p_vec, dtype=np.float32)
                sim = q_arr @ p_arr.T
                out[i] = float(sim.max(axis=1).sum() / max(q_arr.shape[0], 1))
            except Exception:
                out[i] = 0.0
    return out


def _normalize(scores: list[float]) -> list[float]:
    if not scores:
        return scores
    lo, hi = min(scores), max(scores)
    if hi - lo < 1e-9:
        return [0.5] * len(scores)
    return [(s - lo) / (hi - lo) for s in scores]


def fuse_rerank_scores(ce_scores: list[float], colbert_scores: list[float],
                       colbert_weight: float) -> list[float]:
    """Blend cross-encoder and ColBERT late-interaction scores."""
    if not colbert_scores or colbert_weight <= 0:
        return ce_scores
    ce_n = _normalize(ce_scores)
    cb_n = _normalize(colbert_scores)
    w = min(max(colbert_weight, 0.0), 1.0)
    return [(1 - w) * ce + w * cb for ce, cb in zip(ce_n, cb_n)]


def rerank(query, passages, batch_size=64):
    import torch
    tok, mdl, dev = get_reranker()
    scores = []
    for i in range(0, len(passages), batch_size):
        batch = passages[i:i + batch_size]
        pairs = [[query, p] for p in batch]
        inputs = tok(pairs, padding=True, truncation=True, max_length=512,
                     return_tensors="pt").to(dev)
        with torch.no_grad():
            logits = mdl(**inputs).logits.view(-1).float()
        scores.extend(logits.cpu().tolist())
    return scores
