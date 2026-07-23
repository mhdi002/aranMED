# Clinical safety + template-mismatch verification

- When: 2026-07-22 (local)
- Scope: aranmed backend/frontend only (MedicalRAG + hf_asr untouched)

## Automated unit / API tests

Command:

```text
python -m pytest tests/test_clinical_safety.py -v
```

Result: **7/7 passed**

| Case | Result |
|------|--------|
| Critical tension pneumothorax local triage → alerts | PASS |
| Non-critical “no pneumothorax” → empty alerts | PASS |
| Spoken Brain Sonography vs selected `chest` → mismatch | PASS |
| Forbidden spoken title `neck ct` → naming_violations | PASS |
| enrich_report_payload critical + mismatch | PASS |
| POST `/api/report` critical + mismatch fields | PASS |
| POST `/api/report` non-critical matched | PASS |

## Live curl verifier

```text
python scripts/verify_clinical_safety.py
# BASE_URL / BACKEND_URL from env (default http://127.0.0.1:8010)
```

Uses templates `chest` / spoken `Brain Sonography`. Restart the backend after pull so new templates + safety fields are loaded. If the process is stale, `/api/report` may 404 on unknown template ids.

## API fields added

### `POST /api/report` and `POST /api/dictate` (`ReportOut`)
- `critical_alerts`: `[{code, severity, label, excerpt, source, message}, …]`
- `template_mismatch`: `{selected_template_id, selected_official_title, spoken_template, spoken_resolved, matched_template_id, mismatch, naming_violations, selected_wins, message}`
- `patient_id` (optional on request) — when set with critical alerts, logs `channel=clinical` via existing alerts store

### `POST /api/chat` (`ChatOut`)
- `critical_alerts` (local triage over query+answer; merges MedRAG `rule_alerts` when present)

### `POST /api/knowledge/ask`, `POST /api/ehr/{id}/ask`
- `critical_alerts` (same shape); EHR ask also dry-runs `store.log_alert(..., channel="clinical")`

### `POST /api/vision`
- `critical_alerts`

## UI changes
- `CriticalAlertsBanner` + `TemplateMismatchBanner` on Dictate workbench
- Critical banner on Radiology chat and EHR (after Ask MedicalRAG)

## Env (no hardcoded URLs)
- `MEDRAG_API_URL` / `MEDRAG_TIMEOUT_SEC` (existing)
- `CLINICAL_SAFETY_USE_MEDRAG`, `CLINICAL_SAFETY_ALWAYS_MEDRAG`, `CLINICAL_SAFETY_CONFIRM_MEDRAG`
- `REPORT_RULES_PATH`, `REPORT_RULES_DOCX`, `REPORT_RULES_TXT`, `REPORT_RULES_MEDRAG`
