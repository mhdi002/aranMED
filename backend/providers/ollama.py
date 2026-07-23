"""Ollama HTTP provider — supports text, tool-calling and vision (LLaVA, llama3.2-vision, …)."""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Optional

import httpx

from .base import ChatMessage, TextProvider, ToolCall

log = logging.getLogger("provider.ollama")


def _serialise_messages(messages: list[ChatMessage]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        d: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.images:
            d["images"] = [base64.b64encode(b).decode() for b in m.images]
        if m.tool_calls:
            d["tool_calls"] = [
                {
                    "function": {"name": tc.name, "arguments": tc.arguments},
                    "id": tc.id,
                }
                for tc in m.tool_calls
            ]
        if m.tool_call_id:
            d["tool_call_id"] = m.tool_call_id
        if m.name:
            d["name"] = m.name
        out.append(d)
    return out


class OllamaProvider(TextProvider):
    """Talks to a local or remote Ollama daemon via /api/chat."""

    kind = "ollama"
    supports_tools = True

    def __init__(self, *, name: str, config: dict) -> None:
        super().__init__(name=name, config=config)
        import os

        self.host = (
            os.getenv("OLLAMA_HOST")
            or config.get("host")
            or config.get("api_base")
            or ""
        ).rstrip("/")
        if not self.host:
            raise RuntimeError(
                "OLLAMA_HOST is not set (and models.yaml api_base is empty). "
                "See .env.example."
            )
        self.model = (
            config.get("model")
            or os.getenv("OLLAMA_MODEL")
            or ""
        )
        if not self.model:
            raise RuntimeError(
                "OLLAMA_MODEL is not set (and models.yaml model is empty). "
                "See .env.example."
            )
        self.supports_vision = bool(config.get("vision", False))
        self.timeout = float(config.get("timeout", 600))
        self.options = config.get("options", {})         # num_ctx, num_gpu, …
        # Disable Qwen3-style internal "thinking" by default — tool-callers
        # need a deterministic, non-empty `message.content`.
        self.think = bool(config.get("think", False))

    async def _load(self) -> None:
        # Touch the model so it's hot in VRAM. Best-effort.
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(f"{self.host}/api/show", json={"name": self.model})
                r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            log.warning("ollama show %s failed: %s", self.model, e)

    async def health(self) -> dict:
        if not self.host:
            return {"ok": False, "detail": "OLLAMA_HOST unset"}
        try:
            # Short connect/read so /api/health stays snappy when Ollama is slow.
            timeout = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0)
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(f"{self.host}/api/tags")
                r.raise_for_status()
                tags = [m["name"] for m in r.json().get("models", [])]
            return {"ok": True, "available_models": tags, "model": self.model,
                    "model_pulled": any(t.startswith(self.model.split(":")[0]) for t in tags)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)}

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: Optional[list[dict]] = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        stop: Optional[list[str]] = None,
    ) -> ChatMessage:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": _serialise_messages(messages),
            "stream": False,
            "think": self.think,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                **self.options,
            },
        }
        if stop:
            body["options"]["stop"] = stop
        if tools:
            body["tools"] = tools

        url = f"{self.host}/api/chat"
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(url, json=body)
            r.raise_for_status()
            data = r.json()

        msg = data.get("message", {}) or {}
        content = msg.get("content", "") or ""
        # Qwen3/DeepSeek-R1 "thinking" models can put the answer under
        # `message.thinking` and leave `content` empty. Fall back to it.
        thinking = msg.get("thinking", "") or ""
        if not content.strip() and thinking.strip():
            content = thinking.strip()
        # Strip <think>...</think> wrapper if the model leaked it inline.
        if "<think>" in content:
            import re
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip() or content
        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {}) or {}
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args, id=tc.get("id")))
        return ChatMessage(role="assistant", content=content, tool_calls=calls)
