"""HuggingFace vision-language provider (LLaVA, Qwen-VL, MiniCPM-V, etc.).

Uses the universal ``AutoProcessor`` + ``AutoModelForVision2Seq`` /
``AutoModelForCausalLM`` pair with ``trust_remote_code=True`` so it works for
most modern medical-vision LLMs (e.g. *MedDr*, *RadFM*, *LLaVA-Med*).
"""
from __future__ import annotations

import io
import logging
import threading
from typing import Optional

from .base import ChatMessage, ToolCall, VisionProvider

log = logging.getLogger("provider.hf_vision")


class HFVisionProvider(VisionProvider):
    kind = "hf_vision"
    supports_vision = True
    supports_tools = False

    def __init__(self, *, name: str, config: dict) -> None:
        super().__init__(name=name, config=config)
        self.model_id = config["model_id"]
        self.device_pref = config.get("device", "auto")
        self.dtype_name = config.get("dtype", "bfloat16")
        self.trust_remote_code = bool(config.get("trust_remote_code", True))
        self._model = None
        self._proc = None
        self._lock = threading.Lock()

    async def _load(self) -> None:
        import torch
        from transformers import AutoProcessor
        try:
            from transformers import AutoModelForVision2Seq as _AutoVL
        except ImportError:  # very old transformers
            from transformers import AutoModelForCausalLM as _AutoVL  # type: ignore
        dev = "cuda" if (self.device_pref == "auto" and torch.cuda.is_available()) else self.device_pref
        if dev == "auto":
            dev = "cpu"
        dt = {"bfloat16": torch.bfloat16, "float16": torch.float16,
              "float32": torch.float32}.get(self.dtype_name, torch.bfloat16)
        log.info("HF-Vision loading %s on %s (%s)", self.model_id, dev, dt)
        self._proc = AutoProcessor.from_pretrained(
            self.model_id, trust_remote_code=self.trust_remote_code
        )
        self._model = _AutoVL.from_pretrained(
            self.model_id,
            trust_remote_code=self.trust_remote_code,
            torch_dtype=dt if dev == "cuda" else torch.float32,
            device_map=dev,
        ).eval()
        self._device = dev

    async def unload(self) -> None:
        import gc
        self._model = None
        self._proc = None
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
        tools=None,
        temperature: float = 0.2,
        max_tokens: int = 512,
        stop: Optional[list[str]] = None,
    ) -> ChatMessage:
        await self.ensure_loaded()
        # Squash everything into the most recent user turn (most VLMs are
        # single-turn ergonomics-wise).  System prompt is prepended.
        sys = next((m.content for m in messages if m.role == "system"), "")
        last_user = next((m for m in reversed(messages) if m.role == "user"), None)
        if last_user is None:
            return ChatMessage(role="assistant", content="(no user message)")
        prompt = (sys + "\n\n" if sys else "") + (last_user.content or "Describe the image.")

        import anyio
        text = await anyio.to_thread.run_sync(
            self._gen_sync, prompt, last_user.images, temperature, max_tokens
        )
        return ChatMessage(role="assistant", content=text, tool_calls=[])

    def _gen_sync(self, prompt: str, images: list[bytes], temperature: float,
                  max_tokens: int) -> str:
        from PIL import Image
        import torch
        pil_imgs = [Image.open(io.BytesIO(b)).convert("RGB") for b in images]
        with self._lock, torch.inference_mode():
            try:
                inputs = self._proc(  # type: ignore[union-attr]
                    text=prompt, images=pil_imgs or None, return_tensors="pt"
                ).to(self._device)
            except TypeError:
                inputs = self._proc(  # type: ignore[union-attr]
                    prompt, pil_imgs or None, return_tensors="pt"
                ).to(self._device)
            out = self._model.generate(  # type: ignore[union-attr]
                **inputs, max_new_tokens=max_tokens,
                do_sample=temperature > 0,
                temperature=max(temperature, 1e-3),
            )
            in_len = inputs["input_ids"].shape[1] if "input_ids" in inputs else 0
            new = out[0, in_len:] if in_len else out[0]
            return self._proc.batch_decode([new], skip_special_tokens=True)[0].strip()  # type: ignore[union-attr]
