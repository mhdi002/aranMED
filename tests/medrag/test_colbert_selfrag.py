"""Tests for ColBERT score fusion and Self-RAG controller."""
from medrag.index.embedder import fuse_rerank_scores, _normalize
from medrag.rag.self_rag import should_correct


def test_fuse_rerank_scores():
    ce = [0.1, 0.5, 0.9]
    cb = [0.9, 0.5, 0.1]
    fused = fuse_rerank_scores(ce, cb, 0.5)
    assert len(fused) == 3
    assert all(0 <= s <= 1 for s in fused)


def test_normalize_flat():
    assert _normalize([5.0, 5.0, 5.0]) == [0.5, 0.5, 0.5]


def test_should_correct_low_grounding():
    assert should_correct({"grounded": 0.2, "confidence": "low"}) is True


def test_should_correct_high_grounding():
    assert should_correct({"grounded": 0.8, "confidence": "high"}) is False


def test_should_correct_missing_citations():
    assert should_correct({"grounded": 0.9, "confidence": "high", "has_citations": False}) is True


def test_needs_broaden_uses_ce_threshold():
    from medrag.rag.retrieval import _needs_broaden
    from medrag import config as cfg

    # Negative CE logit should broaden when min_ce_score ≈ 0
    assert _needs_broaden([{"rerank_raw": -0.5, "score": 0.9}]) is True
    assert _needs_broaden([{"rerank_raw": 2.5, "score": 2.5}]) is False
    assert float(cfg.MIN_CE_SCORE) > -1.0

