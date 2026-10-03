"""Deterministic OpenAI-compatible chat server for browser E2E runs.

Answers image-bearing requests with fixed "findings" and text requests with a
fixed structured report, so UI flows that call the core/vision models are
reproducible without GPU weights. Run: python fake_llm.py <port>
"""
from __future__ import annotations

import sys
import time

import uvicorn
from fastapi import FastAPI, Request

app = FastAPI()
FINDINGS = "E2E-FINDINGS: Patchy consolidation in the right lower lobe. No pneumothorax."
REPORT = ("CT CHEST\nFINDINGS: Patchy consolidation in the right lower lobe.\n"
          "IMPRESSION: Findings suggest pneumonia (E2E-REPORT). Recommend clinical correlation.")


@app.get("/v1/models")
def models():
    return {"data": [{"id": "e2e-model", "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    has_image = any(isinstance(m.get("content"), list) and
                    any(p.get("type") == "image_url" for p in m["content"])
                    for m in body.get("messages", []))
    text = FINDINGS if has_image else REPORT
    return {"id": "e2e", "object": "chat.completion", "created": int(time.time()),
            "model": body.get("model", "e2e-model"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
