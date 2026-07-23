"""Real end-to-end ASR + LLM test using a synthesised bilingual audio.

Pipeline
--------
1. Synthesise a Persian + English mixed sentence with edge-tts.
2. Transcribe with the real OmniASR provider (omnilingual-asr).
3. Hand the transcript to the live core LLM (Ollama) for a structured
   English radiology report.
4. Separately ping the radiology vision model with a chest X-ray.

These tests are tagged ``live`` and will skip themselves automatically
when the required services / models are missing.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import wave
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from providers.base import ChatMessage  # noqa: E402

pytestmark = pytest.mark.live


# ── bilingual sample ────────────────────────────────────────────────────────
PERSIAN = "یک انورمالی در سگمان تحتانی کبد دیده شده. سایزش ۱ میلی متر هست"
# Mixed sentence to exercise code-switching ASR.
BILINGUAL = (
    "There is یک anomaly در segment تحتانی of the liver. "
    "Its size is ۱ millimeter."
)


# ── helpers ─────────────────────────────────────────────────────────────────
def _http_alive(url: str, timeout: float = 1.5) -> bool:
    try:
        r = httpx.get(url, timeout=timeout)
        return r.status_code < 500
    except Exception:  # noqa: BLE001
        return False


def _have_omniasr() -> bool:
    try:
        import omnilingual_asr  # noqa: F401
        return True
    except ImportError:
        return False


def _omniasr_model_present(card: str = "omniASR_LLM_300M") -> bool:
    # The omnilingual-asr loader caches under HF hub; we accept either
    # the HF cache OR the explicit ~/models snapshot we downloaded.
    candidates = [
        Path.home() / "models" / card,
        Path.home() / "models" / card.replace("_", "-"),
        Path.home() / ".cache" / "huggingface" / "hub"
        / f"models--facebook--{card.replace('_', '-')}",
    ]
    return any(p.exists() and any(p.iterdir()) for p in candidates)


async def _synthesize_mp3(text: str, voice: str = "fa-IR-DilaraNeural") -> bytes:
    """Use edge-tts to synthesise speech (returns mp3 bytes)."""
    import edge_tts
    communicator = edge_tts.Communicate(text, voice=voice)
    buf = bytearray()
    async for chunk in communicator.stream():
        if chunk["type"] == "audio":
            buf.extend(chunk["data"])
    return bytes(buf)


def _mp3_to_wav16k(mp3: bytes) -> bytes:
    """Convert mp3 → 16-kHz mono wav (the format OmniASR expects)."""
    from pydub import AudioSegment
    seg = AudioSegment.from_file(io.BytesIO(mp3), format="mp3")
    seg = seg.set_channels(1).set_frame_rate(16000).set_sample_width(2)
    out = io.BytesIO()
    seg.export(out, format="wav")
    return out.getvalue()


# ────────────────────────────────────────────────────────────────────────────
# Fixture: cache the synthesised wav for the whole module so we don't hit
# edge-tts more than once per pytest run.
# ────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def bilingual_wav() -> bytes:
    cache = ROOT / ".pytest_cache" / "bilingual.wav"
    if cache.exists():
        return cache.read_bytes()
    try:
        mp3 = asyncio.run(_synthesize_mp3(BILINGUAL))
        wav = _mp3_to_wav16k(mp3)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"edge-tts synthesis failed (no internet?): {e}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(wav)
    return wav


@pytest.fixture(scope="module")
def persian_wav() -> bytes:
    cache = ROOT / ".pytest_cache" / "persian.wav"
    if cache.exists():
        return cache.read_bytes()
    try:
        mp3 = asyncio.run(_synthesize_mp3(PERSIAN))
        wav = _mp3_to_wav16k(mp3)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"edge-tts synthesis failed: {e}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(wav)
    return wav


# ────────────────────────────────────────────────────────────────────────────
# 1) Real OmniASR transcription
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_live_omniasr_persian_real(persian_wav):
    if not _have_omniasr():
        pytest.skip("omnilingual-asr package not installed")
    from providers.omniasr import OmniASRProvider

    p = OmniASRProvider(name="omni-test", config={
        "model_card": "omniASR_LLM_300M",
        "default_language": "pes_Arab",
        "batch_size": 1,
    })
    transcript = await p.transcribe(persian_wav, language="fa")
    print("\n[OmniASR Persian] →", transcript)
    assert transcript.strip(), "empty transcript"
    # Loose recall checks — the TTS pronunciation can vary.
    lowered = transcript.lower()
    assert any(w in lowered for w in ("کبد", "liver", "سگمان", "segment",
                                     "انورمالی", "anomaly", "میلی", "milli"))


@pytest.mark.asyncio
async def test_live_omniasr_bilingual_real(bilingual_wav):
    if not _have_omniasr():
        pytest.skip("omnilingual-asr package not installed")
    from providers.omniasr import OmniASRProvider

    p = OmniASRProvider(name="omni-bi", config={
        "model_card": "omniASR_LLM_300M",
        "default_language": "eng_Latn",
        "batch_size": 1,
    })
    transcript = await p.transcribe(bilingual_wav, language="en")
    print("\n[OmniASR bilingual] →", transcript)
    assert transcript.strip(), "empty transcript"


# ────────────────────────────────────────────────────────────────────────────
# 2) Pipe ASR transcript into the live core LLM for structured reporting
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_live_asr_then_core_structured_report(persian_wav):
    if not _have_omniasr():
        pytest.skip("omnilingual-asr not installed")
    OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    if not _http_alive(f"{OLLAMA}/api/tags"):
        pytest.skip(f"Ollama not reachable at {OLLAMA}")

    from providers.omniasr import OmniASRProvider
    from providers.ollama import OllamaProvider

    asr = OmniASRProvider(name="omni", config={
        "model_card": "omniASR_LLM_300M", "default_language": "pes_Arab",
    })
    transcript = await asr.transcribe(persian_wav, language="fa")
    assert transcript.strip()
    print("\n[Persian transcript] →", transcript)

    core = OllamaProvider(name="core", config={
        "host": OLLAMA,
        "model": os.environ.get("OLLAMA_MODEL", "qwen3:14b"),
        "think": False,
        "options": {"num_ctx": 8192, "temperature": 0.1},
    })
    sys_prompt = (
        "You are a radiologist. Translate the user's Persian dictation to "
        "clinical English and produce a concise abdominal-imaging report "
        "with sections FINDINGS and IMPRESSION. If the dictation mentions a "
        "lesion in the liver, describe its size and segment."
    )
    out = await core.chat(
        [ChatMessage(role="system", content=sys_prompt),
         ChatMessage(role="user", content=transcript)],
        temperature=0.1, max_tokens=400,
    )
    print("\n[Structured report] →\n", out.content)
    text = out.content.lower()
    assert out.content.strip(), "empty LLM response"
    assert "liver" in text or "hepatic" in text, out.content
    assert "finding" in text or "impression" in text, out.content


# ────────────────────────────────────────────────────────────────────────────
# 3) Vision model — Radiology-Infer-Mini via llama-server (separate)
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_live_radiology_vision_real():
    base = os.environ.get("LLAMACPP_BASE", "http://127.0.0.1:8088/v1")
    if not _http_alive(f"{base}/models", timeout=2.0):
        pytest.skip(f"llama-server not reachable at {base} "
                    f"— start it with the radiology GGUF + mmproj")

    from providers.openai_compat import OpenAIProvider
    p = OpenAIProvider(name="rad", config={
        "base_url": base,
        "model": os.environ.get("RADIOLOGY_MODEL", "radiology-infer-mini"),
        "vision": True, "timeout": 180,
    })

    # Public chest X-ray sample (Wikimedia Commons), cached locally.
    cache_img = ROOT / ".pytest_cache" / "chest_xray.png"
    if not cache_img.exists() or cache_img.stat().st_size < 10000:
        import subprocess
        cache_img.parent.mkdir(parents=True, exist_ok=True)
        ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120 Safari/537.36")
        url = ("https://upload.wikimedia.org/wikipedia/commons/c/c8/"
               "Chest_Xray_PA_3-8-2010.png")
        try:
            subprocess.run(
                ["curl", "-sL", "-A", ua, "-o", str(cache_img), url],
                check=True, timeout=60,
            )
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"could not fetch sample chest x-ray: {e}")
    img = cache_img.read_bytes()
    assert len(img) > 10000, f"cached image too small: {len(img)}"

    out = await p.chat([
        ChatMessage(role="system", content="You are a radiologist. Be concise."),
        ChatMessage(role="user",
                    content="Briefly describe this chest X-ray in 1–3 sentences.",
                    images=[img]),
    ], temperature=0.1, max_tokens=240)
    print("\n[Radiology vision] →\n", out.content)
    answer = out.content.lower()
    assert out.content.strip(), "empty vision response"
    assert any(w in answer for w in ("chest", "lung", "heart", "thorac",
                                     "x-ray", "cardiac")), out.content
