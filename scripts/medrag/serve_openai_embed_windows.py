"""OpenAI-compatible /v1/embeddings via Starlette (env-driven; Windows-friendly).

Env: VLLM_EMBED_MODEL|MEDRAG_EMBED_MODEL, VLLM_EMBED_HOST|MEDRAG_EMBED_HOST,
     VLLM_EMBED_PORT|MEDRAG_EMBED_PORT, MEDRAG_EMBED_DEVICE, MEDRAG_EMBED_BATCH
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# scripts/medrag/*.py → repo root is parents[2]; scripts/*.py → parents[1]
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1] if (_HERE.parents[1] / "src" / "medrag").is_dir() else _HERE.parent
if not (_ROOT / "src" / "medrag").is_dir() and (_HERE.parent / "src" / "medrag").is_dir():
    _ROOT = _HERE.parent
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
    model_id = _env("VLLM_EMBED_MODEL") or _env("MEDRAG_EMBED_MODEL")
    host = _env("VLLM_EMBED_HOST") or _env("MEDRAG_EMBED_HOST")
    port_s = _env("VLLM_EMBED_PORT") or _env("MEDRAG_EMBED_PORT")
    device = _env("MEDRAG_EMBED_DEVICE") or "cuda"
    if not model_id or not host or not port_s:
        raise SystemExit("Set VLLM_EMBED_MODEL/HOST/PORT (or MEDRAG_EMBED_*) in .env")
    port = int(port_s)

    print(f"[embed-server] loading {model_id} on {device} ...", flush=True)
    from medrag.index._compat import patch_torch_load_check
    patch_torch_load_check()
    from FlagEmbedding import BGEM3FlagModel
    model = BGEM3FlagModel(model_id, use_fp16=(device != "cpu"), devices=device)

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    import uvicorn

    async def models(_request: Request):
        return JSONResponse({
            "object": "list",
            "data": [{"id": model_id, "object": "model", "owned_by": "local"}],
        })

    async def health(_request: Request):
        return JSONResponse({"ok": True, "model": model_id, "device": device})

    async def embeddings(request: Request):
        try:
            payload = await request.json()
        except Exception as e:
            return JSONResponse({"error": f"invalid JSON: {e}"}, status_code=400)
        raw = payload.get("input")
        if raw is None:
            return JSONResponse({"error": "input is required"}, status_code=400)
        texts = raw if isinstance(raw, list) else [raw]
        texts = [str(t) for t in texts if t is not None and str(t).strip()]
        if not texts:
            return JSONResponse({"error": "input is empty"}, status_code=400)
        try:
            out = model.encode(
                texts,
                batch_size=min(len(texts), int(_env("MEDRAG_EMBED_BATCH") or "8")),
                max_length=int(_env("MEDRAG_EMBED_MAX_LENGTH") or "384"),
                return_dense=True,
                return_sparse=False,
                return_colbert_vecs=False,
            )
            dense = out["dense_vecs"]
            data = []
            for i, vec in enumerate(dense):
                arr = vec.tolist() if hasattr(vec, "tolist") else list(vec)
                data.append({"object": "embedding", "index": i, "embedding": arr})
            return JSONResponse({
                "object": "list",
                "data": data,
                "model": payload.get("model") or model_id,
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            })
        except Exception as e:
            return JSONResponse(
                {"error": f"encode failed: {type(e).__name__}: {e}"},
                status_code=500,
            )

    app = Starlette(routes=[
        Route("/v1/models", models, methods=["GET"]),
        Route("/models", models, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/v1/embeddings", embeddings, methods=["POST"]),
        Route("/embeddings", embeddings, methods=["POST"]),
    ])
    print(f"[embed-server] serving {model_id} at http://{host}:{port}/v1", flush=True)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
