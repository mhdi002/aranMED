"""Shared provider contracts."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional


# ---------------------------------------------------------------------------
# Common DTOs
# ---------------------------------------------------------------------------
@dataclass
class ToolCall:
    """A tool invocation requested by the model."""
    name: str
    arguments: dict
    id: Optional[str] = None  # provider-supplied call id (OpenAI-style)


@dataclass
class ChatMessage:
    """One message in a conversation.

    ``role`` is one of ``system``, ``user``, ``assistant``, ``tool``.
    ``content`` is plain text; binary attachments (images, audio) live in
    ``images`` / ``audio`` so each provider can serialise them in its own
    native way (base64 for Ollama, file paths for llama.cpp multimodal, etc).
    """
    role: str
    content: str = ""
    images: list[bytes] = field(default_factory=list)   # PNG/JPEG bytes
    audio: list[bytes] = field(default_factory=list)    # WAV/FLAC bytes
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None  # for role="tool"
    name: Optional[str] = None          # tool name for role="tool"


# ---------------------------------------------------------------------------
# Abstract providers
# ---------------------------------------------------------------------------
class BaseProvider(abc.ABC):
    """Common bookkeeping for all providers."""

    role: str = "generic"
    kind: str = "abstract"

    def __init__(self, *, name: str, config: dict) -> None:
        self.name = name
        self.config = config
        self._loaded = False

    # --- lifecycle ----------------------------------------------------
    async def ensure_loaded(self) -> None:
        """Load model weights / open clients on first use."""
        if self._loaded:
            return
        await self._load()
        self._loaded = True

    async def _load(self) -> None:  # pragma: no cover - default no-op
        return

    async def unload(self) -> None:
        """Release resources (used by the registry's memory governor)."""
        self._loaded = False

    @abc.abstractmethod
    async def health(self) -> dict:
        """Return ``{ok: bool, detail: str, …}``."""

    # --- introspection ------------------------------------------------
    def info(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "kind": self.kind,
            "loaded": self._loaded,
            "config": {k: v for k, v in self.config.items() if k != "api_key"},
        }


class TextProvider(BaseProvider):
    """LLM that consumes :class:`ChatMessage`\\s and produces text + optional
    tool calls.  Vision-capable text providers also accept ``images`` on the
    user message and should override :pyattr:`supports_vision`.
    """

    role = "text"
    supports_vision: bool = False
    supports_tools: bool = False

    @abc.abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: Optional[list[dict]] = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        stop: Optional[list[str]] = None,
    ) -> ChatMessage:
        """Single-turn completion. Returns the assistant message."""

    async def stream(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> AsyncIterator[str]:  # pragma: no cover - optional
        """Default: yield the full response once. Providers may override."""
        msg = await self.chat(messages, **kwargs)
        yield msg.content


class VisionProvider(TextProvider):
    """Marker subclass — same API as TextProvider but ``supports_vision=True``."""
    role = "vision"
    supports_vision = True


class ASRProvider(BaseProvider):
    role = "asr"

    @abc.abstractmethod
    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        language: Optional[str] = None,
        sample_rate: Optional[int] = None,
        filename_hint: str = "",
    ) -> str:
        """Return the transcription as plain text.

        ``filename_hint`` (the original upload filename, e.g. ``dictation.m4a``)
        lets the decoder pick the right codec path for containers that
        ``soundfile`` cannot read natively (m4a/mp3/aac) instead of relying on
        a lucky fallback.
        """
