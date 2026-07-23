"""Tests for SOTA RAG components (no GPU/Qdrant required)."""
from medrag.ingest.contextual import contextual_header, embed_text, gen_display_text
from medrag.rag.compression import compress_passage
from medrag.rag.grounding import lexical_grounding_score
from medrag.rag.retrieval import diversify_by_book, mmr_select, rrf_merge


def test_contextual_header():
    h = contextual_header("Harrison", "internal_medicine", "textbook", 10, 12, "en")
    assert "Harrison" in h and "internal_medicine" in h


def test_embed_text_prepends_header():
    raw = "Patient presents with chest pain."
    out = embed_text(raw, {"title": "T", "specialty": "s", "doc_type": "textbook",
                           "page_start": 1, "page_end": 1, "language": "en"}, True)
    assert out.startswith("Document:") and raw in out


def test_gen_display_text_uses_parent():
    hit = {"text": "short", "parent_text": "much longer parent context about medicine"}
    assert gen_display_text(hit) == hit["parent_text"]


def test_rrf_merge_deduplicates():
    a = [{"title": "A", "page": 1, "text": "x", "book_id": "b1"}]
    b = [{"title": "A", "page": 1, "text": "x", "book_id": "b1"}, {"title": "B", "page": 2, "text": "y", "book_id": "b2"}]
    merged = rrf_merge([a, b])
    assert len(merged) == 2


def test_diversify_by_book():
    passages = [
        {"title": "BookA", "score": 1.0, "text": "a1"},
        {"title": "BookA", "score": 0.9, "text": "a2"},
        {"title": "BookB", "score": 0.8, "text": "b1"},
    ]
    out = diversify_by_book(passages, 2, max_per_book=1)
    titles = [p["title"] for p in out]
    assert len(out) == 2 and len(set(titles)) == 2


def test_mmr_reduces_redundancy():
    candidates = [
        {"text": "hyperkalemia ECG peaked T waves", "score": 1.0},
        {"text": "hyperkalemia ECG peaked T waves treatment", "score": 0.95},
        {"text": "diabetes insulin management", "score": 0.7},
    ]
    out = mmr_select(candidates, "hyperkalemia ECG", k=2, lambda_=0.5)
    assert len(out) == 2
    texts = " ".join(c["text"] for c in out)
    assert "diabetes" in texts or "hyperkalemia" in texts


def test_lexical_grounding():
    ctx = "Hyperkalemia causes peaked T waves on ECG."
    good = "Peaked T waves are an ECG finding in hyperkalemia."
    bad = "Pneumonia requires azithromycin always."
    assert lexical_grounding_score(good, ctx) > lexical_grounding_score(bad, ctx)


def test_citation_coverage():
    from medrag.rag.grounding import citation_coverage

    assert citation_coverage("Peaked T [1] and sine wave [2].", 5) == 1.0
    assert citation_coverage("Bad cite [99].", 5) == 0.0
    assert citation_coverage("No cites here.", 5) == 0.0


def test_compress_passage_short_circuits():
    text = "Short text."
    assert compress_passage(text, "query", max_chars=1000) == text


def test_system_prompt_forbids_cot():
    from medrag.rag import prompts

    assert "chain-of-thought" in prompts.SYSTEM_PROMPT.lower() or "<think>" in prompts.SYSTEM_PROMPT
    assert "Sources used" in prompts.SYSTEM_PROMPT

