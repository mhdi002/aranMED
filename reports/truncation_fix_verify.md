# Truncation fix verification

**Date:** 2026-07-22  
**Samples:** Desktop `Feb 3*.m4a` (22.4s / 57.0s / 41.9s)

## Root cause

1. **Whisper internal long-form seek** (`return_timestamps` on the full file) stopped mid-file on mixed Persian/English dictation — e.g. last chunk timestamp **~28s on a 57s file**, **~14s on a 42s file**.
2. **Per-window early stop:** even 30s short-form windows without timestamp tokens often halt after a fraction of the window (e.g. ~90–127 chars vs ~380+ with timestamps).

## Fix (env-configurable, no hardcoding)

| Setting | Default | Role |
|---------|---------|------|
| `WHISPER_MAX_SHORTFORM_S` | 30 | Above this duration → sliding windows |
| `WHISPER_CHUNK_LENGTH_S` | 30 | Window length (seconds) |
| `WHISPER_STRIDE_LENGTH_S` | 0 | Overlap between windows |
| `TRITON_TIMEOUT_SEC` | 300 | Client timeout for long clips |

**Code paths**

- `deploy/triton/compat_http_server.py` — consecutive windows + `return_timestamps=True` per window; clear conflicting `forced_decoder_ids`
- `deploy/triton/model_repository/whisper/1/model.py` — same
- `backend/providers/triton_asr.py` — duration logging; timeout/env already configurable
- `backend/providers/hf_asr.py` — **minimal** long-form-only change: same window join + timestamps; short-form path unchanged; no radiology prompt on mid-file windows

## Before → after (completeness)

| File | Dur | Before (broken) | After Triton | After hf_asr |
|------|-----|-----------------|--------------|--------------|
| 5.12 | 22.4s | OK short-form ~257 / 241 chars | 258 chars — start→end | 241 chars — start→end |
| 5.15 | 57.0s | Triton/hf seek stopped ~**28s** (686 / 731 chars of *partial* file) | **537 chars full file** — START patient/CPR … END pleural/vein | **690 chars full file** — START Hello/Ali … END pleural / Thank you |
| 5.38 | 41.9s | Seek stopped ~**14s** (379 / 404 chars partial) | **403 chars full file** — START patient … END hematoma/collection | **398 chars full file** — START Hamad … END hematoma/collection |

Note: after char counts on 5.15 Triton can be *lower* than the old partial transcript because the old text densified English into the first ~28s only. **Coverage** (beginning + ending clinical anchors) is the completeness metric.

## Evidence anchors

### 5.15 (57s) — must include intro AND closing findings
- Triton START: patient / CPR / portable  
- Triton END: bilateral pleural / vein-side closing  
- hf START: `Hello, welcome to Ali Bakhsh…`  
- hf END: `…pleural effusion… Thank you.`

### 5.38 (42s)
- Triton/hf START: Hamad / Abdo Pelvik / FreeFluid  
- Triton/hf END: hematoma vs early collection  

Machine-readable: `reports/truncation_fix_verify.json`
