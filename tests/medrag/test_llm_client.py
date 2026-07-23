"""Unit tests for LLM provider config + OpenAI-compatible URL helpers."""
from __future__ import annotations


def test_openai_base_normalization():
    from medrag.rag import routing

    assert routing._openai_base("http://localhost:8000") == "http://localhost:8000/v1"
    assert routing._openai_base("http://localhost:8000/v1") == "http://localhost:8000/v1"
    assert routing._openai_base("http://localhost:8000/v1/") == "http://localhost:8000/v1"
    assert routing._openai_base("http://host:8000/v1/chat/completions") == "http://host:8000/v1"


def test_llm_config_exports():
    from medrag import config as cfg

    assert cfg.LLM_PROVIDER in ("vllm", "ollama", "openai")
    assert cfg.LLM_BASE_URL
    assert cfg.LLM_MODEL
    assert cfg.EMBED_PROVIDER in ("local", "vllm", "openai")
    assert cfg.EMBED_MODEL
    if cfg.EMBED_PROVIDER in ("vllm", "openai"):
        assert cfg.EMBED_BASE_URL
    assert cfg.OLLAMA_URL
    # Ollama host must not be the OpenAI-compat path
    assert not cfg.OLLAMA_URL.rstrip("/").endswith("/v1")
    assert cfg.LLM_MAX_TOKENS > 0
    assert cfg.LLM_ENABLE_THINKING is False
    assert cfg.LLM_STRIP_THINKING is True
    assert float(cfg.MIN_CE_SCORE) > -1.0  # broaden not muted


def test_llm_unavailable_message_mentions_vllm():
    from medrag.rag.routing import _friendly_unreachable

    msg = _friendly_unreachable("vllm", "http://localhost:8000/v1", ConnectionError("refused"))
    assert "vLLM" in msg
    assert "MEDRAG_LLM_BASE_URL" in msg or "VLLM_BASE_URL" in msg


def test_embed_openai_base_helper():
    from medrag.index.embedder import _openai_embed_base

    assert _openai_embed_base("http://localhost:8001") == "http://localhost:8001/v1"
    assert _openai_embed_base("http://localhost:8001/v1") == "http://localhost:8001/v1"


def test_strip_thinking_removes_qwen_blocks():
    from medrag.rag.routing import strip_thinking

    raw = (
        "<think>\nI need to analyze passage [1] carefully...\n</think>\n\n"
        "Peaked T waves are the earliest ECG sign of hyperkalemia [2].\n"
        "Sources used: [2]"
    )
    out = strip_thinking(raw)
    assert "<think>" not in out.lower()
    assert "Peaked T waves" in out
    assert "I need to analyze" not in out


def test_strip_thinking_cot_lead():
    from medrag.rag.routing import strip_thinking

    raw = (
        "The user is asking for ECG findings in hyperkalemia based on the provided context.\n"
        "I need to extract relevant information from the provided passages.\n\n"
        "Based on the context, peaked T waves are earliest [2].\n"
        "Sources used: [2]"
    )
    out = strip_thinking(raw)
    assert "The user is asking" not in out
    assert "peaked" in out.lower() or "Based on" in out


def test_strip_thinking_process_dump():
    from medrag.rag.routing import strip_thinking

    raw = "Thinking Process:\n\n1. Analyze the request...\n\nBased on the context, peaked T [1]."
    out = strip_thinking(raw)
    assert "Thinking Process" not in out
    assert "peaked" in out.lower()
