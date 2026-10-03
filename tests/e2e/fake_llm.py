"""Deterministic OpenAI-compatible chat server for browser E2E runs.

Answers image-bearing requests with fixed "findings" and text requests with a
fixed structured report, so UI flows that call the core/vision models are
reproducible without GPU weights. Run: python fake_llm.py <port>
"""
from __future__ import annotations

import json
import sys
import time

import uvicorn
from fastapi import FastAPI, Request

app = FastAPI()
FINDINGS = "E2E-FINDINGS: Patchy consolidation in the right lower lobe. No pneumothorax."
REPORT = ("CT CHEST\nFINDINGS: Patchy consolidation in the right lower lobe.\n"
          "IMPRESSION: Findings suggest pneumonia (E2E-REPORT). Recommend clinical correlation.")

# Answer for the legacy "build an EHR from free text" prompt, so the EHR and
# alerts pages have a real record (with a dose due) to show.
EHR = json.dumps({
    "patient": {"name": "Maryam Ahmadi", "age": 54, "sex": "F", "mrn": "E2E-EHR-1", "weight_kg": 68},
    "encounter": {"date": "2026-10-02", "chief_complaint": "Productive cough and fever for 4 days",
                  "summary": "Community-acquired pneumonia, right lower lobe."},
    "problems": [{"name": "Community-acquired pneumonia", "status": "active"},
                 {"name": "Type 2 diabetes mellitus", "status": "active"}],
    "allergies": [{"substance": "Penicillin", "reaction": "Rash"}],
    "medications": [
        {"name": "Azithromycin", "dose": "500 mg", "route": "PO", "frequency": "daily",
         "frequency_hours": 24, "indication": "pneumonia", "notes": None},
        {"name": "Metformin", "dose": "1000 mg", "route": "PO", "frequency": "twice daily",
         "frequency_hours": 12, "indication": "diabetes", "notes": None}],
    "vitals": {"bp": "128/76", "hr": 96, "temp_c": 38.4, "spo2": 94, "rr": 20},
    "notes": "Chest CT shows right lower lobe consolidation."})


def _is_ehr_prompt(messages) -> bool:
    for m in messages:
        c = m.get("content")
        if m.get("role") == "system" and isinstance(c, str) and "JSON Electronic Health Record" in c:
            return True
    return False


@app.get("/v1/models")
def models():
    return {"data": [{"id": "e2e-model", "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    has_image = any(isinstance(m.get("content"), list) and
                    any(p.get("type") == "image_url" for p in m["content"])
                    for m in body.get("messages", []))
    text = FINDINGS if has_image else (EHR if _is_ehr_prompt(body.get("messages", [])) else REPORT)
    return {"id": "e2e", "object": "chat.completion", "created": int(time.time()),
            "model": body.get("model", "e2e-model"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
