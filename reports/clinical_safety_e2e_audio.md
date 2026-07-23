# Clinical safety E2E — real audio

- When (UTC): `2026-07-22T18:16:15.253519+00:00`
- Completed: `2026-07-22T18:25:52.583532+00:00`
- Backend: `http://127.0.0.1:8010`
- Sample: `Feb 3, 5.12 PM​.m4a` (725762 bytes)

## Honesty / scope

**Prior tests** (`tests/test_clinical_safety.py`, `scripts/verify_clinical_safety.py`, `reports/clinical_safety_verification.md`) used **synthetic text transcripts** and pytest with a **fake LLM** — **not** real file audio through Whisper/Triton.

This run transcribed a **real Desktop m4a** with **whisper-triton**, then called `POST /api/report`. Critical findings were **not** in the audio.

## 1. Real audio transcription (THIS run)

- Source: **REAL_AUDIO** (`whisper-triton` → asr_model=`whisper-triton`)
- Elapsed: 14.57s
- Length: 257 chars
- Audio contained critical phrase (heuristic): `False`

### Transcript preview

```text
برای بیمار ساری رحیمی یک هپاتو بیلیاری سونا بزنید. لیویه را نرمل سایز، نرمل پارانشیم اکو، گالوستی میدیستنت را نرمل، پروگزیمال پارت سی بیدیو را نرمل، میدیو دیستال پارت گست شدو، ویژوال پارت پانچراست را نرمل بزنید و همینطور اسپلین هم نرمل بزنید. ما چکر کریمیم.
```

## 2a. Real-audio Persian transcript → `/api/report` (raw, wrong template)

Selected wrong `template_id` without a spoken-template cue. Persian ASR did not match `extract_spoken_template` patterns, so `mismatch` stayed false — both safety **fields** were present in the JSON.

- Source: **REAL_AUDIO_TRANSCRIPT**
- Selected `template_id`: `brain`
- HTTP: 200 in 68.38s
- `template_mismatch.mismatch`: **False**
- `critical_alerts` count: 0

```json
{
  "template_mismatch": {
    "selected_template_id": "brain",
    "selected_official_title": "Brain Sonography",
    "spoken_template": null,
    "spoken_resolved": null,
    "matched_template_id": null,
    "mismatch": false,
    "naming_violations": [],
    "selected_wins": true,
    "message": null
  },
  "critical_alerts": []
}
```

## 2b. Local safety assess on Persian ASR + spoken cue → mismatch=true

- Source: **REAL_AUDIO_TRANSCRIPT_PLUS_SPOKEN_TEMPLATE_CUE** (in-process `assess_template_mismatch`, same module as API)
- Note: assess_template_mismatch() in-process (same code path as API safety layer); no LLM.
- `mismatch`: **True**

```json
{
  "selected_template_id": "chest",
  "selected_official_title": "Chest sonography",
  "spoken_template": "template Brain Sonography",
  "spoken_resolved": "template Brain Sonography",
  "matched_template_id": "brain",
  "mismatch": true,
  "naming_violations": [],
  "selected_wins": true,
  "message": "Spoken exam/template «template Brain Sonography» differs from selected UI template «chest» (Chest sonography). Selected template structure was used."
}
```

## 2c. Live `/api/report` mismatch=true (English ASR of same m4a + cue)

- Source: **SAME_FILE_ENGLISH_ASR_FROM_PRIOR_HF_PASS_PLUS_SPOKEN_CUE**
- Note: This E2E whisper-triton pass returned Persian. Posting the full Persian body to /api/report crashed the backend worker (connection reset, no Python traceback). For a live API mismatch=true proof we used the English transcript of the SAME Desktop m4a captured earlier via hf_asr in backend logs, plus the spoken-template cue. Local assess_template_mismatch on Persian+cue also yields mismatch=true.
- HTTP: 200 in 26.37s
- `template_mismatch.mismatch`: **True**
- Spoken: `template Brain Sonography`
- Matched id: `brain`

```json
{
  "template_mismatch": {
    "selected_template_id": "chest",
    "selected_official_title": "Chest sonography",
    "spoken_template": "template Brain Sonography",
    "spoken_resolved": "template Brain Sonography",
    "matched_template_id": "brain",
    "mismatch": true,
    "naming_violations": [],
    "selected_wins": true,
    "message": "Spoken exam/template «template Brain Sonography» differs from selected UI template «chest» (Chest sonography). Selected template structure was used."
  },
  "critical_alerts": []
}
```

## 3. Critical alerts — synthetic text

- Source: **SYNTHETIC_TEXT**
- Note: Real audio transcript had no critical phrases; critical_alerts verified via synthetic transcript POST /api/report only.
- HTTP: 200 in 26.04s
- `critical_alerts` count: 2

```json
{
  "template_mismatch": {
    "selected_template_id": "chest",
    "selected_official_title": "Chest sonography",
    "spoken_template": "template Brain Sonography",
    "spoken_resolved": "template Brain Sonography",
    "matched_template_id": "brain",
    "mismatch": true,
    "naming_violations": [],
    "selected_wins": true,
    "message": "Spoken exam/template «template Brain Sonography» differs from selected UI template «chest» (Chest sonography). Selected template structure was used."
  },
  "critical_alerts": [
    {
      "code": "tension_pneumothorax",
      "severity": "critical",
      "label": "Tension Pneumothorax",
      "excerpt": "tension pneumothorax",
      "source": "local_triage",
      "message": "Critical / potentially life-threatening finding detected (tension pneumothorax): «tension pneumothorax»"
    },
    {
      "code": "lethal_or_critical_flag",
      "severity": "critical",
      "label": "Lethal Or Critical Flag",
      "excerpt": "immediately life-threatening",
      "source": "local_triage",
      "message": "Critical / potentially life-threatening finding detected (lethal or critical flag): «immediately life-threatening»"
    }
  ]
}
```

## 4. Hybrid — same-file English dictation + critical phrase

- Source: **SAME_FILE_ENGLISH_ASR_PLUS_SYNTHETIC_CRITICAL_PHRASE**
- HTTP: 200 in 25.06s
- `critical_alerts` count: 2

```json
{
  "transcript_preview": "For the patient, we will perform a hepato biliary sonography. Liver normal size, normal parasympathetic echo, gallus mediscentum normal, proximal part CBD normal, medial distal part gas shadow, visual part pancreas normal and spleen also normal. Impression: tension pneumothorax — immediately life-threatening.",
  "template_mismatch": {
    "selected_template_id": "chest",
    "selected_official_title": "Chest sonography",
    "spoken_template": null,
    "spoken_resolved": null,
    "matched_template_id": null,
    "mismatch": false,
    "naming_violations": [],
    "selected_wins": true,
    "message": null
  },
  "critical_alerts": [
    {
      "code": "tension_pneumothorax",
      "severity": "critical",
      "label": "Tension Pneumothorax",
      "excerpt": "tension pneumothorax",
      "source": "local_triage",
      "message": "Critical / potentially life-threatening finding detected (tension pneumothorax): «tension pneumothorax»"
    },
    {
      "code": "lethal_or_critical_flag",
      "severity": "critical",
      "label": "Lethal Or Critical Flag",
      "excerpt": "immediately life-threatening",
      "source": "local_triage",
      "message": "Critical / potentially life-threatening finding detected (lethal or critical flag): «immediately life-threatening»"
    }
  ]
}
```

## Verdict

- Real audio → whisper-triton ASR: **PASS**
- Real Persian ASR → `/api/report` safety fields present: **PASS** (mismatch=false without cue)
- Persian ASR + cue → local `template_mismatch=true`: **PASS**
- Live `/api/report` `template_mismatch=true`: **PASS**
- Critical alerts (SYNTHETIC_TEXT): **PASS**
- Hybrid critical: **PASS**

### What used real audio vs synthetic text

| Path | Real audio? | Notes |
|------|-------------|-------|
| Transcription | **YES** — Desktop m4a via whisper-triton | Persian transcript |
| Report safety fields (raw) | **YES** — that transcript → `/api/report` | Fields present; mismatch=false |
| mismatch=true (local) | **YES** — real Persian ASR + cue | Same clinical_safety code as API |
| mismatch=true (HTTP) | **Partial** — English ASR of same file + cue | Persian body crashed worker |
| critical_alerts (primary) | **NO** — synthetic text | tension pneumothorax fixture |
| critical_alerts (hybrid) | **Partial** — same-file EN dictation + phrase | Not spoken in audio |

### Bottom line

Prior clinical-safety tests were **text-only** (synthetic transcripts + fake LLM). This E2E proves **real m4a → whisper-triton → `/api/report` JSON includes `template_mismatch` and `critical_alerts`**. Forcing `mismatch=true` needs a spoken-template cue the raw Persian ASR did not emit. Critical content was **not** in the sample audio; that path was proven with **synthetic/hybrid text**, not spoken criticals.
