"""HuggingFace transformers text-LLM provider.

Loads any causal LM (Qwen, Llama, Mistral, …) via ``AutoModelForCausalLM``.
Tool calls are best-effort: we ask the model to output strict JSON when
``tools`` is provided and parse the first JSON object.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

from .base import ChatMessage, TextProvider, ToolCall

log = logging.getLogger("provider.hf_text")


def _extract_first_json(s: str) -> Optional[dict]:
    """Naively pull the outermost JSON object out of an LLM's reply."""
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None


class HFTextProvider(TextProvider):
    kind = "hf_text"
    supports_tools = True   # JSON-prompted

    def __init__(self, *, name: str, config: dict) -> None:
        super().__init__(name=name, config=config)
        self.model_id = config["model_id"]
        self.device = config.get("device", "auto")
        self.dtype = config.get("dtype", "bfloat16")
        self.trust_remote_code = bool(config.get("trust_remote_code", True))
        self._model = None
        self._tok = None
        self._lock = threading.Lock()

    async def _load(self) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        dtype = {"bfloat16": torch.bfloat16, "float16": torch.float16,
                 "float32": torch.float32}.get(self.dtype, torch.bfloat16)
        log.info("HF loading %s (device=%s, dtype=%s)", self.model_id, self.device, self.dtype)
        self._tok = AutoTokenizer.from_pretrained(
            self.model_id, trust_remote_code=self.trust_remote_code
        )
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_id,
            trust_remote_code=self.trust_remote_code,
            torch_dtype=dtype,
            device_map=self.device,
        ).eval()

    async def unload(self) -> None:
        import gc
        self._model = None
        self._tok = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        await super().unload()

    async def health(self) -> dict:
        return {"ok": True, "model_id": self.model_id, "loaded": self._loaded}

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
        msgs = [{"role": m.role, "content": m.content} for m in messages]
        if tools:
            tools_descr = json.dumps(tools, indent=2)
            sys_extra = (
                "\n\nYou MAY call tools. Available tools (JSON-Schema):\n"
                f"{tools_descr}\n"
                "To call a tool reply with ONLY a JSON object: "
                '{"tool": "<name>", "arguments": {…}}. '
                "Otherwise answer normally."
            )
            if msgs and msgs[0]["role"] == "system":
                msgs[0]["content"] += sys_extra
            else:
                msgs.insert(0, {"role": "system", "content": sys_extra.strip()})

        import anyio
        text = await anyio.to_thread.run_sync(
            self._generate_sync, msgs, temperature, max_tokens, stop
        )

        calls: list[ToolCall] = []
        if tools:
            obj = _extract_first_json(text)
            if obj and isinstance(obj, dict) and "tool" in obj:
                calls.append(ToolCall(name=str(obj["tool"]),
                                      arguments=obj.get("arguments", {})))
                text = ""  # consumed
        return ChatMessage(role="assistant", content=text, tool_calls=calls)

    def _generate_sync(self, msgs: list[dict], temperature: float,
                       max_tokens: int, stop: Optional[list[str]]) -> str:
        import torch
        with self._lock, torch.inference_mode():
            ids = self._tok.apply_chat_template(  # type: ignore[union-attr]
                msgs, add_generation_prompt=True, return_tensors="pt"
            ).to(self._model.device)              # type: ignore[union-attr]
            out = self._model.generate(            # type: ignore[union-attr]
                ids,
                max_new_tokens=max_tokens,
                temperature=max(temperature, 1e-3),
                do_sample=temperature > 0,
                pad_token_id=self._tok.eos_token_id,  # type: ignore[union-attr]
            )
            new = out[0, ids.shape[1]:]
            return self._tok.decode(new, skip_special_tokens=True).strip()  # type: ignore[union-attr]
