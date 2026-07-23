from medrag.ingest.chunk import chunk_text_paragraph as chunk_text


def test_chunk_text_respects_size_budget():
    sentence = "This is a medical sentence about clinical guidelines. "
    text = sentence * 200
    chunks = list(chunk_text(text, approx_tokens=100, overlap=20))
    assert len(chunks) > 1
    max_chars = 100 * 4
    assert all(len(c) <= max_chars + len(sentence) for c in chunks)


def test_chunk_text_empty_input():
    assert list(chunk_text("")) == []
    assert list(chunk_text("   \n\n  ")) == []


def test_chunk_text_short_text_single_chunk():
    text = "Short guideline note."
    chunks = list(chunk_text(text, approx_tokens=800, overlap=120))
    assert chunks == ["Short guideline note."]
