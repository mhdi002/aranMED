"""FastAPI server for MedicalRAG."""
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


def serve(host=None, port=None):
    from fastapi import FastAPI
    from pydantic import BaseModel
    import uvicorn

    host = host if host is not None else API_HOST
    port = port if port is not None else API_PORT

    app = FastAPI(title="MedicalRAG")
    eng = RagEngine()

    class Q(BaseModel):
        query: str
        specialty: str | None = None

    class ImageQ(BaseModel):
        query: str = ""
        image_path: str

    @app.post("/ask")
    def ask(q: Q):
        return eng.answer(q.query, specialty=q.specialty)

    @app.post("/ask/image")
    def ask_image(q: ImageQ):
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

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    serve()
