"""FastAPI server for MedicalRAG.

Exposes a module-level ASGI ``app`` so the service can be served by any
process manager (``uvicorn medrag.interfaces.api:app``, gunicorn workers,
multiple container replicas behind a load balancer) — not only by the
in-process :func:`serve` helper. The RAG engine is constructed lazily on
first use so importing this module (e.g. for a health probe or during image
build) never loads models.
"""
from __future__ import annotations

import threading

from fastapi import FastAPI
from pydantic import BaseModel

from medrag.config import (
    API_HOST,
    API_PORT,
    EMBED_MODEL,
    EMBED_PROVIDER,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_PROVIDER,
)
from medrag.index import vectorstore as vs
from medrag.rag.engine import RagEngine

_engine: RagEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> RagEngine:
    """Lazily build one RagEngine per process (thread-safe)."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = RagEngine()
    return _engine


class Q(BaseModel):
    query: str
    specialty: str | None = None


class ImageQ(BaseModel):
    query: str = ""
    image_path: str


def create_app() -> FastAPI:
    app = FastAPI(title="MedicalRAG")

    @app.post("/ask")
    def ask(q: Q):
        return get_engine().answer(q.query, specialty=q.specialty)

    @app.post("/ask/image")
    def ask_image(q: ImageQ):
        eng = get_engine()
        if q.image_path.endswith((".png", ".jpg", ".jpeg")):
            return eng.answer_question_image(q.image_path)
        return eng.answer(q.query, image_path=q.image_path)

    @app.get("/health")
    def health():
        from medrag.index.embedder import embed_health
        from medrag.rag.routing import llm_health

        return {
            "chunks": vs.count(),
            "llm": {
                "provider": LLM_PROVIDER,
                "model": LLM_MODEL,
                "base_url": LLM_BASE_URL,
                **llm_health(),
            },
            "embeddings": {
                "provider": EMBED_PROVIDER,
                "model": EMBED_MODEL,
                **embed_health(),
            },
        }

    @app.get("/live")
    def live():
        """Liveness only — never touches Qdrant/models, so a load balancer can
        distinguish 'process up' from 'dependencies ready' (/health)."""
        return {"ok": True}

    return app


app = create_app()


def serve(host=None, port=None):
    """Run the service directly (``medrag-serve``)."""
    import uvicorn

    uvicorn.run(
        app,
        host=host if host is not None else API_HOST,
        port=port if port is not None else API_PORT,
    )


if __name__ == "__main__":
    serve()
