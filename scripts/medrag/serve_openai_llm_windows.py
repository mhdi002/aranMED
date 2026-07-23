"""OpenAI-compatible /v1/chat/completions via Starlette (env-driven; Windows-friendly).

Prefer Docker/Linux `vllm serve` in production — native Windows pip vLLM lacks vllm._C.

Env (required): VLLM_MODEL|MEDRAG_LLM_MODEL, VLLM_HOST|MEDRAG_LLM_HOST, VLLM_PORT|MEDRAG_LLM_PORT
Optional: MEDRAG_LLM_DTYPE, MEDRAG_LLM_MAX_NEW_TOKENS, MEDRAG_LLM_LOAD_4BIT
"""
from __future__ import annotations

import os
import re
import sys
import time
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def main() -> None:
    _load_dotenv(_ROOT / ".env")
    model_id = _env("VLLM_MODEL") or _env("MEDRAG_LLM_MODEL")
    host = _env("VLLM_HOST") or _env("MEDRAG_LLM_HOST")
    port_s = _env("VLLM_PORT") or _env("MEDRAG_LLM_PORT")
    dtype_s = (_env("MEDRAG_LLM_DTYPE") or "float16").lower()
    max_new = int(_env("MEDRAG_LLM_MAX_NEW_TOKENS") or "512")
    use_4bit = (_env("MEDRAG_LLM_LOAD_4BIT") or "0").lower() in ("1", "true", "yes")
    if not model_id or not host or not port_s:
        raise SystemExit("Set VLLM_MODEL/HOST/PORT (or MEDRAG_LLM_*) in .env")
    port = int(port_s)

    import torch
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from transformers import AutoModelForCausalLM, AutoTokenizer
    import uvicorn

    from medrag.index._compat import patch_torch_load_check
    patch_torch_load_check()

    print(f"[llm-server] loading {model_id} (4bit={use_4bit}, dtype={dtype_s}) ...", flush=True)
    tok = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    load_kw: dict = {"trust_remote_code": True, "device_map": "auto"}
    if use_4bit:
        try:
            from transformers import BitsAndBytesConfig
            load_kw["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.float16,
            )
        except Exception as e:
            print(f"[llm-server] 4bit unavailable ({e}); dtype fallback", flush=True)
            use_4bit = False
    if not use_4bit:
        dtype = {"float16": torch.float16, "bfloat16": torch.bfloat16, "auto": "auto"}.get(
            dtype_s, torch.float16
        )
        load_kw["torch_dtype"] = dtype
    mdl = AutoModelForCausalLM.from_pretrained(model_id, **load_kw)
    mdl.eval()

    async def models(_request: Request):
        return JSONResponse({
            "object": "list",
            "data": [{"id": model_id, "object": "model", "owned_by": "local"}],
        })

    async def health(_request: Request):
        return JSONResponse({"ok": True, "model": model_id, "backend": "transformers"})

    async def chat(request: Request):
        try:
            payload = await request.json()
        except Exception as e:
            return JSONResponse({"error": f"invalid JSON: {e}"}, status_code=400)
        messages = payload.get("messages") or []
        temperature = float(payload.get("temperature") or 0.1)
        gen_n = int(payload.get("max_tokens") or max_new)
        # Prefer request flag; default False so RAG answers are not CoT dumps
        enable_thinking = False
        ctk = payload.get("chat_template_kwargs") or {}
        if isinstance(ctk, dict) and "enable_thinking" in ctk:
            enable_thinking = bool(ctk["enable_thinking"])
        elif "enable_thinking" in payload:
            enable_thinking = bool(payload["enable_thinking"])
        try:
            prompt = tok.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=enable_thinking,
            )
        except TypeError:
            try:
                prompt = tok.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True,
                )
            except Exception:
                prompt = "\n".join(
                    f"{m.get('role')}: {m.get('content')}" for m in messages
                ) + "\nassistant:"
        except Exception:
            prompt = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in messages) + "\nassistant:"
        inputs = tok(prompt, return_tensors="pt")
        device = next(mdl.parameters()).device
        inputs = {k: v.to(device) for k, v in inputs.items()}
        with torch.no_grad():
            out = mdl.generate(
                **inputs,
                max_new_tokens=gen_n,
                do_sample=temperature > 0,
                temperature=max(temperature, 1e-5),
                pad_token_id=tok.eos_token_id,
            )
        new_tokens = out[0][inputs["input_ids"].shape[-1]:]
        text = tok.decode(new_tokens, skip_special_tokens=True)
        # Strip leaked <think> / plain-text thinking dumps
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.I).strip()
        if "</think>" in text.lower():
            text = re.split(r"</think>", text, flags=re.I)[-1].strip()
        if re.match(r"^(?:Thinking Process:|Thinking:)", text, re.I):
            m = re.search(
                r"\n\n((?:Based on|According to|Sources used|Final answer:|\*\*).*)",
                text, re.I | re.S,
            )
            text = m.group(1).strip() if m else ""
        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model") or model_id,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    app = Starlette(routes=[
        Route("/v1/models", models, methods=["GET"]),
        Route("/models", models, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/v1/chat/completions", chat, methods=["POST"]),
        Route("/chat/completions", chat, methods=["POST"]),
    ])
    print(f"[llm-server] serving {model_id} at http://{host}:{port}/v1", flush=True)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
