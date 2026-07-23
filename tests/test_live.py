"""Live functional tests against the *real* services if they're up.

These tests are skipped automatically when the services aren't reachable, so
``pytest`` always passes on a clean checkout.

Run them explicitly with::

    pytest tests/test_live.py -v -s

Required services per test (each guarded with its own skip):

* test_live_ollama_core / test_live_ollama_e2e
    A local Ollama daemon at $OLLAMA_HOST (default 127.0.0.1:11434) with
    the ``qwen3.5:9b`` (or whatever ``models.yaml`` says) model pulled.

* test_live_radiology_vision
    A llama-server at http://127.0.0.1:8088/v1 serving
    ``Radiology-Infer-Mini-Q8_0.gguf`` + the matching ``mmproj`` file.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from providers.base import ChatMessage  # noqa: E402
from providers.ollama import OllamaProvider  # noqa: E402
from providers.openai_compat import OpenAIProvider  # noqa: E402

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:14b")
LLAMACPP_BASE = os.environ.get("LLAMACPP_BASE", "http://127.0.0.1:8088/v1")
RADIOLOGY_MODEL = os.environ.get("RADIOLOGY_MODEL", "radiology-infer-mini")


def _http_alive(url: str, timeout: float = 1.5) -> bool:
    try:
        r = httpx.get(url, timeout=timeout)
        return r.status_code < 500
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Ollama core LLM
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_live_ollama_core():
    if not _http_alive(f"{OLLAMA_HOST}/api/tags"):
        pytest.skip(f"Ollama not reachable at {OLLAMA_HOST}")
    p = OllamaProvider(name="live-ollama",
                       config={"host": OLLAMA_HOST, "model": OLLAMA_MODEL})
    h = await p.health()
    assert h["ok"], h
    out = await p.chat(
        [ChatMessage(role="system",
                     content="Reply with the single word: PONG. No punctuation."),
         ChatMessage(role="user", content="ping")],
        temperature=0.0, max_tokens=8,
    )
    assert "pong" in out.content.lower(), f"got: {out.content!r}"


@pytest.mark.asyncio
async def test_live_ollama_persian_to_english_report():
    """Real radiology workflow: Persian sentence → structured English IMPRESSION."""
    if not _http_alive(f"{OLLAMA_HOST}/api/tags"):
        pytest.skip(f"Ollama not reachable at {OLLAMA_HOST}")
    from registry import Registry
    Registry._instance = None
    reg = Registry(Path(__file__).resolve().parents[1] / "backend" / "models.yaml")
    core = await reg.get_text("core")

    sample = "یک انورمالی در سگمان فوقانی کبد دیده شد"
    sys = ChatMessage(role="system",
                      content=("You are a radiologist. Translate the Persian "
                               "dictation to clinical English and produce a "
                               "two-line FINDINGS / IMPRESSION block."))
    out = await core.chat(
        [sys, ChatMessage(role="user", content=sample)],
        temperature=0.1, max_tokens=400,
    )
    text = out.content.lower()
    assert "hepatic" in text or "liver" in text, out.content
    assert "impression" in text or "finding" in text, out.content


# ---------------------------------------------------------------------------
# Radiology vision LLM (GGUF via llama-server)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_live_radiology_vision_chest_xray():
    if not _http_alive(f"{LLAMACPP_BASE}/models", timeout=2.0):
        pytest.skip(f"llama-server (radiology vision) not reachable at {LLAMACPP_BASE}")
    p = OpenAIProvider(name="live-radiology", config={
        "base_url": LLAMACPP_BASE, "model": RADIOLOGY_MODEL, "vision": True,
    })

    # Use a public chest X-ray sample image (NIH CXR set sample).
    img_url = ("https://upload.wikimedia.org/wikipedia/commons/thumb/c/c8/"
               "Chest_X-ray_normal.jpg/640px-Chest_X-ray_normal.jpg")
    try:
        img = httpx.get(img_url, timeout=10).content
        assert len(img) > 1000
    except Exception:  # noqa: BLE001
        pytest.skip("could not fetch sample CXR image")

    out = await p.chat([
        ChatMessage(role="system",
                    content="You are a radiologist. Be concise."),
        ChatMessage(role="user",
                    content="Briefly describe the chest X-ray. 1–3 sentences.",
                    images=[img]),
    ], temperature=0.1, max_tokens=200)
    answer = out.content.lower()
    assert any(w in answer for w in ("chest", "lung", "heart", "x-ray", "thora")), out.content


# ---------------------------------------------------------------------------
# Full agent loop with a real core LLM (Ollama) + fake ASR/vision
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_live_agent_e2e_with_real_core():
    if not _http_alive(f"{OLLAMA_HOST}/api/tags"):
        pytest.skip(f"Ollama not reachable at {OLLAMA_HOST}")
    # We swap in fake ASR+vision but keep the *real* core LLM, so we exercise
    # the actual tool-call protocol that Ollama emits.
    import textwrap
    from registry import Registry
    yaml_path = Path(__file__).resolve().parents[1] / "backend" / "models.live.yaml"
    yaml_path.write_text(textwrap.dedent(f"""
        models:
          - role: core
            name: live-core
            provider: ollama
            enabled: true
            default: true
            config:
              host: {OLLAMA_HOST}
              model: {OLLAMA_MODEL}
              options: {{num_ctx: 8192, temperature: 0.1}}
          - role: asr
            name: fake-asr
            provider: fake_asr
            enabled: true
            default: true
            config: {{text: "Mild bibasilar atelectasis. No focal consolidation."}}
          - role: vision
            name: fake-vision
            provider: fake_vision
            enabled: true
            default: true
            config: {{}}
        runtime:
          max_warm_models: 3
    """).strip())

    Registry._instance = None
    reg = Registry(yaml_path)

    from agent import Agent
    from memory import MemoryStore
    a = Agent(registry=reg, memory=MemoryStore())

    # Build a dummy 1-second wav.
    from tests.conftest import make_wav  # type: ignore[import-not-found]
    wav = make_wav()

    res = await a.run(
        session_id="live-e2e",
        user_text=("I uploaded an audio dictation. Please transcribe it, then "
                   "draft a structured CT chest report using the right template."),
        attachments={"audio:0": wav},
        max_iters=6,
    )
    # The real LLM should have called transcribe_audio at minimum.
    names = [c["name"] for c in res.tool_calls]
    assert "transcribe_audio" in names, f"tool calls were: {names}"
    # And produced *some* report text.
    assert res.answer or res.state.get("report"), res
