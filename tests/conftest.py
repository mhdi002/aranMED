"""Pytest fixtures shared by the test suite.

We register a *fake* provider plugin into the registry's class table and
write a tiny ``models.test.yaml`` so every test gets a deterministic core /
asr / vision triple without needing GPU weights.

Tests that explicitly want to hit the real services are marked
``@pytest.mark.live`` and will be skipped unless their endpoint is up.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import wave
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

# Force a deterministic registry yaml during tests.
os.environ.setdefault("ASR_AGENT_REGISTRY_YAML", str(BACKEND / "models.test.yaml"))

# ---------------------------------------------------------------------------
# Fake provider implementations
# ---------------------------------------------------------------------------
from providers.base import (  # noqa: E402
    ASRProvider,
    ChatMessage,
    TextProvider,
    ToolCall,
    VisionProvider,
)


class FakeCoreLLM(TextProvider):
    """Scripted core LLM that emits deterministic tool calls + answers."""
    kind = "fake_core"
    supports_tools = True

    def __init__(self, *, name: str, config: dict) -> None:
        super().__init__(name=name, config=config)
        # Each script entry is either:
        #   ("answer", "<text>")
        #   ("tool",   name, arguments_dict)
        self.script: list[tuple] = list(config.get("script") or [])
        self.received: list[list[ChatMessage]] = []

    async def health(self) -> dict:
        return {"ok": True}

    async def chat(self, messages, *, tools=None, temperature=0.2,
                   max_tokens=1024, stop=None):
        self.received.append(list(messages))
        if not self.script:
            return ChatMessage(role="assistant", content="(fake) done")
        kind, *rest = self.script.pop(0)
        if kind == "tool":
            name, args = rest
            return ChatMessage(role="assistant", content="",
                               tool_calls=[ToolCall(name=name, arguments=args, id="c1")])
        return ChatMessage(role="assistant", content=rest[0])


class FakeASR(ASRProvider):
    kind = "fake_asr"

    def __init__(self, *, name: str, config: dict) -> None:
        super().__init__(name=name, config=config)
        self.text = config.get("text", "یک انورمالی در سگمان فوقانی کبد دیده شد")

    async def health(self) -> dict:
        return {"ok": True}

    async def transcribe(self, audio_bytes: bytes, *, language=None,
                         sample_rate=None, filename_hint: str = "") -> str:
        assert audio_bytes  # we got *some* bytes
        return self.text


class FakeVision(VisionProvider):
    kind = "fake_vision"

    def __init__(self, *, name: str, config: dict) -> None:
        super().__init__(name=name, config=config)
        self.answer = config.get(
            "answer",
            "Bilateral perihilar opacities. No pneumothorax. "
            "Heart size within normal limits. Recommend clinical correlation.",
        )

    async def health(self) -> dict:
        return {"ok": True}

    async def chat(self, messages, *, tools=None, temperature=0.2,
                   max_tokens=512, stop=None):
        # Sanity: the agent must pass at least one image.
        last_user = next((m for m in reversed(messages) if m.role == "user"), None)
        assert last_user is not None and last_user.images, \
            "vision provider was called without an image"
        return ChatMessage(role="assistant", content=self.answer)


# ---------------------------------------------------------------------------
# Test models.yaml
# ---------------------------------------------------------------------------
TEST_YAML = """\
models:
  - role: core
    name: fake-core
    provider: fake_core
    enabled: true
    default: true
    config:
      script: []
  - role: asr
    name: fake-asr
    provider: fake_asr
    enabled: true
    default: true
    config: {}
  - role: vision
    name: fake-vision
    provider: fake_vision
    enabled: true
    default: true
    config: {}
runtime:
  max_warm_models: 2
  vram_budget_gb: 0
  history_keep_turns: 4
  history_summary_trigger: 8
"""

(BACKEND / "models.test.yaml").write_text(TEST_YAML, encoding="utf-8")


# ---------------------------------------------------------------------------
# Inject fake providers into the registry's class map and patch its yaml path
# ---------------------------------------------------------------------------
import registry as registry_mod  # noqa: E402

registry_mod.PROVIDER_CLS["fake_core"] = FakeCoreLLM
registry_mod.PROVIDER_CLS["fake_asr"] = FakeASR
registry_mod.PROVIDER_CLS["fake_vision"] = FakeVision


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    """Each test gets its own Registry singleton bound to models.test.yaml."""
    registry_mod.Registry._instance = None
    inst = registry_mod.Registry(BACKEND / "models.test.yaml")
    monkeypatch.setattr(registry_mod.Registry, "_instance", inst)
    yield inst
    registry_mod.Registry._instance = None


@pytest.fixture
def fake_core(fresh_registry) -> FakeCoreLLM:
    return fresh_registry._providers["fake-core"]   # noqa: SLF001


@pytest.fixture
def fake_asr(fresh_registry) -> FakeASR:
    return fresh_registry._providers["fake-asr"]    # noqa: SLF001


@pytest.fixture
def fake_vision(fresh_registry) -> FakeVision:
    return fresh_registry._providers["fake-vision"] # noqa: SLF001


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------
def make_wav(seconds: float = 1.0, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * int(sr * seconds))
    return buf.getvalue()


def make_png(w: int = 32, h: int = 32, color: tuple = (180, 180, 180)) -> bytes:
    from PIL import Image
    img = Image.new("RGB", (w, h), color)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def wav_bytes() -> bytes:
    return make_wav()


@pytest.fixture
def png_bytes() -> bytes:
    return make_png()


# ---------------------------------------------------------------------------
# Fresh SQLite database for each test (auth, EHR, alerts, education)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """Bind the DB module to a throwaway file under the test's tmp_path."""
    import db
    original = db.get_db_path()
    db.set_db_path(tmp_path / "test_app.db")
    yield db.get_db_path()
    db.set_db_path(original)


@pytest.fixture
def http_client():
    """FastAPI TestClient with router mounted; lazily imported to inherit fresh_db."""
    from fastapi.testclient import TestClient
    import app as app_mod
    return TestClient(app_mod.app)


@pytest.fixture
def auth_headers(http_client):
    """Register a new user and return Authorization headers."""
    r = http_client.post("/api/auth/register",
                         json={"username": "tester", "password": "testpass",
                               "role": "doctor"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
