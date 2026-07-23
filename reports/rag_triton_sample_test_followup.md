# RAG + Triton sample suite — follow-up

**Date:** 2026-07-22  
**Root cause (Triton 500):** Whisper rejects audio **> ~30 s** unless long-form mode is enabled (`return_timestamps=True`).  
- `Feb 3, 5.12 PM.m4a` = **22.4 s** → previously PASS  
- `Feb 3, 5.15 PM.m4a` = **57.0 s** → previously FAIL (HTTP 500 in ~1.6 s)  
- `Feb 3, 5.38 PM.m4a` = **41.9 s** → previously FAIL (HTTP 500 in ~0.8 s)

Server log (`triton_compat.err.log`):

```text
ValueError: You have passed more than 3000 mel input features (> 30 seconds)
which automatically enables long-form generation which requires the model to
predict timestamp tokens. Please either pass `return_timestamps=True` ...
```

## Fix (MedicalRAG / hf_asr untouched)

1. `deploy/triton/compat_http_server.py` — detect `wav > 30 * sr`, set `return_timestamps=True` (+ `condition_on_prev_tokens`).
2. `deploy/triton/model_repository/whisper/1/model.py` — same long-form rule for real Triton Python backend.
3. `backend/providers/triton_asr.py` + `models.yaml` / `models.docker.yaml` — default `TRITON_TIMEOUT_SEC` **120 → 300** (long clips on CPU need more than 2 minutes).

## Before → after (ASR)

| File | Duration | Triton before | Triton after | hf_asr |
|------|----------|---------------|--------------|--------|
| 5.12 PM.m4a | 22.4 s | PASS 68.1 s / 257 chars | PASS 60.6 s / 257 chars | PASS |
| 5.15 PM.m4a | 57.0 s | **FAIL 500** (1.6 s) | **PASS 112.4 s / 686 chars** | PASS |
| 5.38 PM.m4a | 41.9 s | **FAIL 500** (0.8 s) | **PASS 74.8 s / 379 chars** | PASS |

Machine-readable results: `reports/rag_triton_sample_test_followup.json`

## Optional RAG radiology retry

Retried `/ask` specialty=radiology with **900 s** client timeout → still **FAIL timed out** (unchanged; not an ASR issue).
