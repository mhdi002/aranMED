"""llama.cpp provider.

Two flavours auto-selected by config:

* ``server`` — talks to ``llama-server`` over its OpenAI-compatible HTTP API.
  This is the recommended path; it gives us the same streaming + tool-call
  semantics as Ollama and reuses :class:`OpenAIProvider`.

* ``python`` — embedded ``llama_cpp.Llama`` instance, useful for offline tests
  or when no server is running.  Tool calls are best-effort (we parse the
  model's JSON output).  Vision models are supported via an mmproj file.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Optional

from .base import ChatMessage, TextProvider, ToolCall
from .openai_compat import OpenAIProvider

log = logging.getLogger("provider.llamacpp")

# Map friendly handler names (used in models.yaml) to llama_cpp classes.
_VISION_HANDLERS = {
    "llava15":   "Llava15ChatHandler",
    "llava16":   "Llava16ChatHandler",
    "llava":     "Llava15ChatHandler",   # alias
    "moondream": "MoondreamChatHandler",
    "nanollava": "NanollavaChatHandler",
    "qwen2vl":   "Qwen2VLChatHandler",
    "qwen25":    "Qwen25VLChatHandler",
    "minicpmv":  "MiniCPMv26ChatHandler",
}


def _build_vision_handler(handler_name: str, mmproj_path: str):
    """Instantiate the right llama_cpp chat handler for a vision model."""
    import llama_cpp.llama_chat_format as fmt
    cls_name = _VISION_HANDLERS.get(handler_name.lower(), "Llava15ChatHandler")
    cls = getattr(fmt, cls_name)
    return cls(clip_model_path=mmproj_path, verbose=False)


def _serialise_messages_vision(messages: list[ChatMessage]) -> list[dict]:
    """Convert ChatMessage list → OpenAI multimodal format understood by
    llama-cpp-python vision chat handlers."""
    out = []
    for m in messages:
        if m.images:
            # Build a multipart content list: text + image_url blocks
            parts: list[dict] = []
            if m.content:
                parts.append({"type": "text", "text": m.content})
            for img_bytes in m.images:
                b64 = base64.b64encode(img_bytes).decode()
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                })
            out.append({"role": m.role, "content": parts})
        else:
            out.append({"role": m.role, "content": m.content or ""})
    return out


class LlamaCppServerProvider(OpenAIProvider):
    """Thin alias so users can write ``provider: llamacpp_server`` in YAML."""
    kind = "llamacpp_server"


class LlamaCppPythonProvider(TextProvider):
    """In-process llama.cpp via the ``llama-cpp-python`` package.

    For plain text models set only ``model_path``.
    For vision models also set ``mmproj_path`` and ``vision_handler``.

    Supported vision_handler values:
        llava15 (default), llava16, moondream, nanollava, qwen2vl, minicpmv
    """
    kind = "llamacpp_python"
    supports_tools = True  # JSON-grammar best-effort

    def __init__(self, *, name: str, config: dict) -> None:
        super().__init__(name=name, config=config)
        self.model_path = os.path.expanduser(config["model_path"])
        self.n_ctx = int(config.get("n_ctx", 8192))
        self.n_gpu_layers = int(config.get("n_gpu_layers", -1))
        self.chat_format = config.get("chat_format")        # e.g. "chatml"
        # Vision-specific
        self.mmproj_path: Optional[str] = config.get("mmproj_path")
        self.vision_handler: str = config.get("vision_handler", "llava15")
        self._is_vision = bool(self.mmproj_path)
        # Explicitly mark the provider as vision-capable for the registry
        self.vision_capable = self._is_vision
        self._llm = None

    async def supports_vision(self) -> bool:
        """Return True if this provider can handle images."""
        return self._is_vision

    async def _load(self) -> None:
        from llama_cpp import Llama
        log.info(
            "llama.cpp loading %s (ctx=%d, gpu_layers=%d, vision=%s)",
            self.model_path, self.n_ctx, self.n_gpu_layers, self._is_vision,
        )
        kwargs: dict[str, Any] = {
            "model_path": self.model_path,
            "n_ctx": self.n_ctx,
            "n_gpu_layers": self.n_gpu_layers,
            "verbose": False,
        }
        if self._is_vision:
            # Vision models need a chat handler; chat_format must NOT be set
            # alongside a handler or llama-cpp-python will raise.
            kwargs["chat_handler"] = _build_vision_handler(
                self.vision_handler,
                os.path.expanduser(self.mmproj_path),  # type: ignore[arg-type]
            )
            kwargs["logits_all"] = True   # required by llava handlers
        else:
            if self.chat_format:
                kwargs["chat_format"] = self.chat_format

        self._llm = Llama(**kwargs)

    async def unload(self) -> None:
        self._llm = None
        await super().unload()

    async def health(self) -> dict:
        return {
            "ok": os.path.exists(self.model_path),
            "path": self.model_path,
            "vision": self._is_vision,
            "mmproj": self.mmproj_path,
        }

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: Optional[list[dict]] = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        stop: Optional[list[str]] = None,
    ) -> ChatMessage:
        await self.ensure_loaded()

        # Use multimodal serialisation for vision models, plain for text
        if self._is_vision:
            oai_messages = _serialise_messages_vision(messages)
        else:
            oai_messages = [{"role": m.role, "content": m.content or ""} for m in messages]

        kwargs: dict[str, Any] = {
            "messages": oai_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stop": stop or [],
        }
        if tools and not self._is_vision:
            # Tool calling not supported alongside vision handlers
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
            kwargs["tool_choice"] = "auto"

        # llama-cpp-python is synchronous; run in a thread
        import anyio
        result = await anyio.to_thread.run_sync(
            lambda: self._llm.create_chat_completion(**kwargs)  # type: ignore[union-attr]
        )
        choice = result["choices"][0]["message"]
        calls: list[ToolCall] = []
        for tc in choice.get("tool_calls") or []:
            fn = tc.get("function", {}) or {}
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments", "")}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args, id=tc.get("id")))
        return ChatMessage(role="assistant", content=choice.get("content") or "", tool_calls=calls)