"""Advanced retrieval: multi-query fusion, MMR, corrective search."""
from __future__ import annotations

import re

from medrag.config import (
    COLBERT_ENABLED, COLBERT_TOP_K, COLBERT_WEIGHT,
    MAX_CHUNKS_PER_BOOK, MIN_CE_SCORE, MIN_RERANK_SCORE, MMR_ENABLED, MMR_LAMBDA,
    RERANK_ENABLED, RETRIEVAL, RRF_K,
)
from medrag.index import embedder, vectorstore as vs


def _token_set(text: str) -> set[str]:
    return set(re.findall(r"[\w\u0600-\u06FF]+", text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def rrf_merge(result_lists: list[list[dict]], k: int | None = None) -> list[dict]:
    k = k or RRF_K
    scores: dict[str, float] = {}
    payloads: dict[str, dict] = {}
    for lst in result_lists:
        for rank, p in enumerate(lst):
            key = f"{p.get('book_id', '')}:{p.get('title', '')}:{p.get('page', '')}:{hash(p.get('text', '')[:80])}"
            scores[key] = scores.get(key, 0) + 1.0 / (k + rank + 1)
            payloads[key] = p
    ranked = sorted(scores, key=scores.get, reverse=True)
    return [payloads[i] for i in ranked]


def diversify_by_book(passages: list[dict], limit: int, max_per_book: int) -> list[dict]:
    seen: dict[str, int] = {}
    out = []
    for p in passages:
        key = p.get("book_id") or p.get("title") or p.get("book", "?")
        if seen.get(key, 0) >= max_per_book:
            continue
        seen[key] = seen.get(key, 0) + 1
        out.append(p)
        if len(out) >= limit:
            break
    if len(out) < limit:
        for p in passages:
            if p in out:
                continue
            out.append(p)
            if len(out) >= limit:
                break
    return out


def mmr_select(candidates: list[dict], query: str, k: int,
               lambda_: float | None = None) -> list[dict]:
    """Maximal Marginal Relevance using rerank scores + lexical similarity."""
    if not MMR_ENABLED or len(candidates) <= k:
        return candidates[:k]
    lambda_ = lambda_ if lambda_ is not None else MMR_LAMBDA

    rel_scores = [c.get("score", 0.0) for c in candidates]
    if max(rel_scores) > min(rel_scores):
        lo, hi = min(rel_scores), max(rel_scores)
        rel_norm = [(s - lo) / (hi - lo + 1e-9) for s in rel_scores]
    else:
        rel_norm = [1.0] * len(candidates)

    token_sets = [_token_set(c.get("text", "")) for c in candidates]
    selected: list[int] = []
    remaining = set(range(len(candidates)))

    while len(selected) < k and remaining:
        best_i, best_score = None, -1e9
        for i in remaining:
            rel = rel_norm[i]
            if selected:
                sim = max(_jaccard(token_sets[i], token_sets[j]) for j in selected)
            else:
                sim = 0.0
            mmr = lambda_ * rel - (1 - lambda_) * sim
            if mmr > best_score:
                best_score, best_i = mmr, i
        selected.append(best_i)
        remaining.remove(best_i)
    return [candidates[i] for i in selected]


def _search_one(query_vec: dict, specialties, language, top_k: int,
                filters: dict | None = None) -> list[dict]:
    filters = filters or {}
    hits = vs.hybrid_search(
        query_vec["dense"], query_vec["sparse"],
        specialties=specialties, language=language, top_k=top_k,
        source_corpus=filters.get("source_corpus"),
        doc_types=filters.get("doc_types"),
        index_tags=filters.get("index_tags"),
        country=filters.get("country"),
        active=filters.get("active"),
    )
    return [dict(h) for h in hits]


def _rerank_and_weight(query: str, hits: list[dict]) -> list[dict]:
    if not hits:
        return []
    tw = RETRIEVAL.get("type_weights", {})
    texts = [h.get("parent_text") or h["text"] for h in hits]

    if RERANK_ENABLED:
        ce_scores = embedder.rerank(query, texts)
    else:
        ce_scores = [0.0] * len(hits)

    # ColBERT late interaction on top pool
    colbert_k = min(COLBERT_TOP_K, len(hits))
    cb_full = [0.0] * len(hits)
    if COLBERT_ENABLED and colbert_k > 0:
        cb_scores = embedder.colbert_rerank(query, texts[:colbert_k])
        cb_full[:colbert_k] = cb_scores
        fused = embedder.fuse_rerank_scores(ce_scores, cb_full, COLBERT_WEIGHT)
    else:
        fused = ce_scores

    ranked = []
    for fused_s, ce_s, cb_s, h in zip(fused, ce_scores, cb_full, hits):
        dt = h.get("doc_type", "textbook")
        ranked.append({
            **h,
            "score": float(fused_s) * tw.get(dt, 1.0),
            "rerank_raw": float(ce_s),
            "colbert_raw": float(cb_s),
        })
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked


def retrieve(query: str, search_queries: list[str] | None = None,
             hyde_text: str | None = None, specialties=None, language=None,
             top_k=None, final_k=None, top_k_boost: int = 0,
             filters: dict | None = None) -> list[dict]:
    top_k = (top_k or RETRIEVAL["top_k_search"]) + top_k_boost
    final_k = final_k or RETRIEVAL["top_k_final"]
    queries = search_queries or [query]

    # Encode all query variants (+ optional HyDE doc)
    encode_texts = list(queries)
    if hyde_text:
        encode_texts.append(hyde_text)
    vecs = embedder.encode_queries(encode_texts)

    result_lists = []
    for i, qv in enumerate(vecs[: len(queries)]):
        result_lists.append(_search_one(qv, specialties, language, top_k, filters))
    if hyde_text and len(vecs) > len(queries):
        result_lists.append(_search_one(vecs[-1], specialties, language, top_k, filters))

    merged = rrf_merge(result_lists)
    ranked = _rerank_and_weight(query, merged[:top_k])
    pool_k = max(final_k * 2, final_k + 4)
    diversified = diversify_by_book(ranked, pool_k, MAX_CHUNKS_PER_BOOK)
    return mmr_select(diversified, query, final_k)


def _needs_broaden(hits: list[dict]) -> bool:
    """True when top hit is weak. Prefer CE logits; fused ColBERT scores are ≈[0,1]."""
    if not hits:
        return True
    top = hits[0]
    if "rerank_raw" in top:
        return float(top["rerank_raw"]) < float(MIN_CE_SCORE)
    return float(top.get("score", 0)) < float(MIN_RERANK_SCORE)


def corrective_retrieve(query: str, search_queries: list[str] | None = None,
                        hyde_text: str | None = None, specialties=None,
                        language=None, final_k=None, top_k_boost: int = 0,
                        filters: dict | None = None) -> list[dict]:
    """Two-stage retrieval: filtered first, then broaden if low confidence."""
    final_k = final_k or RETRIEVAL["top_k_final"]
    hits = retrieve(query, search_queries, hyde_text, specialties, language,
                    final_k=final_k, top_k_boost=top_k_boost, filters=filters)
    if not _needs_broaden(hits):
        return hits
    # Broaden: drop intent filters AND language so FA queries can hit EN library/Mehrsys
    broad = retrieve(query, search_queries, hyde_text, None, None,
                     final_k=final_k, top_k_boost=top_k_boost, filters=None)
    if not hits:
        return broad
    if broad and broad[0].get("score", 0) > hits[0].get("score", -999):
        return broad
    return hits
