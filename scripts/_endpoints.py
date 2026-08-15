"""Single source of truth for service endpoints used by ops/verification scripts.

Every URL is resolved from the environment (repo-root ``.env`` is loaded when
python-dotenv is available), so no script needs to embed a host or port.
Defaults mirror ``.env.example`` and exist only so a bare checkout still runs;
override any of them via environment variables.

See docs/core/CONFIGURATION.md for the full configuration surface.
"""
from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Load repo-root .env if python-dotenv is installed (optional dependency for
# plain scripts; the services themselves always load it via their own config).
try:  # pragma: no cover - trivial
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
    load_dotenv(_ROOT / ".env.example")  # documented defaults only
except Exception:  # noqa: BLE001
    pass


def _url(*names: str, default: str) -> str:
    """First non-empty env var among *names*, else *default* (trailing / stripped)."""
    for n in names:
        v = (os.getenv(n) or "").strip()
        if v:
            return v.rstrip("/")
    return default.rstrip("/")


def _host_port(host_env: str, port_env: str, *, default_host: str, default_port: str) -> str:
    host = (os.getenv(host_env) or default_host).strip().rstrip("/")
    port = (os.getenv(port_env) or default_port).strip()
    if "://" not in host:
        host = f"http://{host}"
    return f"{host}:{port}"


# aranmed backend (FastAPI)
BACKEND_URL = _url(
    "ASR_API_BASE", "BACKEND_URL", "ARANMED_BACKEND_URL",
    default="http://127.0.0.1:8010",
)

# Next.js frontend
FRONTEND_URL = _url("FRONTEND_URL", default=_host_port(
    "FRONTEND_HOST", "FRONTEND_PORT", default_host="127.0.0.1", default_port="3000",
))

# MedicalRAG microservice
MEDRAG_URL = _url("MEDRAG_API_URL", "MEDICALRAG_URL", default="http://127.0.0.1:8080")

# Qdrant vector DB
QDRANT_URL = _url("QDRANT_URL", default="http://127.0.0.1:6333")

# Ollama (fallback / core LLM host)
OLLAMA_URL = _url("OLLAMA_HOST", default="http://127.0.0.1:11434")

# vLLM OpenAI-compatible generation + embeddings endpoints
LLM_BASE_URL = _url("MEDRAG_LLM_BASE_URL", "VLLM_BASE_URL", default="http://127.0.0.1:8000/v1")
EMBED_BASE_URL = _url("MEDRAG_EMBED_BASE_URL", default="http://127.0.0.1:8001/v1")

# Triton Inference Server (optional additive ASR provider)
TRITON_URL = _url("TRITON_URL", default="http://127.0.0.1:8002")

# Load balancer / public entrypoint (see deploy/nginx). Falls back to the
# frontend when no gateway is configured.
GATEWAY_URL = _url("GATEWAY_URL", "PUBLIC_URL", default=FRONTEND_URL)


def all_endpoints() -> dict[str, str]:
    """Everything resolved above — handy for `--print-endpoints` style output."""
    return {
        "backend": BACKEND_URL,
        "frontend": FRONTEND_URL,
        "gateway": GATEWAY_URL,
        "medrag": MEDRAG_URL,
        "qdrant": QDRANT_URL,
        "ollama": OLLAMA_URL,
        "llm": LLM_BASE_URL,
        "embeddings": EMBED_BASE_URL,
        "triton": TRITON_URL,
    }


if __name__ == "__main__":  # pragma: no cover
    import json

    print(json.dumps(all_endpoints(), indent=2))
