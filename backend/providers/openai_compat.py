"""OpenAI-compatible HTTP provider.

Works with: OpenAI, Azure, vLLM (`--openai-mode`), llama.cpp's
``server --api-like-OAI``, LM Studio, text-generation-webui's OAI extension,
TGI, etc.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Optional

import httpx

from .base import ChatMessage, TextProvider, ToolCall

log = logging.getLogger("provider.openai")


def _serialise(messages: list[ChatMessage]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m.images and m.role == "user":
            parts: list[dict] = [{"type": "text", "text": m.content}] if m.content else []
            for b in m.images:
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{base64.b64encode(b).decode()}"},
                })
            out.append({"role": "user", "content": parts})
            continue
        d: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            d["tool_calls"] = [
                {"id": tc.id or f"call_{i}", "type": "function",
                 "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                for i, tc in enumerate(m.tool_calls)
            ]
            d["content"] = m.content or None
        if m.tool_call_id:
            d["tool_call_id"] = m.tool_call_id
        if m.name:
            d["name"] = m.name
        out.append(d)
    return out


class OpenAIProvider(TextProvider):
    kind = "openai"
    supports_tools = True

    #: Where requests go when no endpoint is configured.
    PUBLIC_DEFAULT = "https://api.openai.com/v1"

    def __init__(self, *, name: str, config: dict) -> None:
        super().__init__(name=name, config=config)
        # `api_base` is accepted as an alias because it is the name most other
        # OpenAI-compatible tooling uses, and a config that spells it that way
        # would otherwise be ignored in silence.
        endpoint = config.get("base_url") or config.get("api_base")
        if not endpoint:
            # Defaulting to the public API is the wrong direction to fail in a
            # self-hosted clinical deployment: a typo'd key name would send
            # patient text to a third party, and the only symptom is a 401 --
            # or none at all, if a key happens to be present. Say so loudly.
            log.warning(
                "provider %r has no base_url/api_base configured — falling back to "
                "the PUBLIC endpoint %s. If this is meant to be a local vLLM or "
                "other self-hosted server, set base_url; otherwise clinical text "
                "will be sent off-machine.",
                name, self.PUBLIC_DEFAULT,
            )
            endpoint = self.PUBLIC_DEFAULT
        self.base_url = endpoint.rstrip("/")
        self.model = config["model"]
        self.api_key = config.get("api_key", "sk-no-key-required")
        self.timeout = float(config.get("timeout", 600))
        self.supports_vision = bool(config.get("vision", False))

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(f"{self.base_url}/models",
                                headers={"Authorization": f"Bearer {self.api_key}"})
                r.raise_for_status()
            return {"ok": True, "model": self.model}
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
            "messages": _serialise(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if stop:
            body["stop"] = stop
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
            body["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.base_url}/chat/completions",
                             headers={"Authorization": f"Bearer {self.api_key}"},
                             json=body)
            r.raise_for_status()
            data = r.json()

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {}) or {}
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments", "")}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args, id=tc.get("id")))
        return ChatMessage(role="assistant", content=msg.get("content") or "", tool_calls=calls)
