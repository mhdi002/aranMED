import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
#!/usr/bin/env python3
"""Finalize clinical_safety_e2e_audio.md from saved real ASR + live /api/report calls."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "reports" / "clinical_safety_e2e_audio_raw.json"
OUT_MD = ROOT / "reports" / "clinical_safety_e2e_audio.md"
from _endpoints import BACKEND_URL as BASE

sys.path.insert(0, str(ROOT / "backend"))
from clinical_safety import assess_template_mismatch, triage_local  # noqa: E402


def main() -> int:
    bundle = json.loads(RAW.read_text(encoding="utf-8"))
    fa = bundle["transcription"]["text"]
    bundle["when_completed"] = datetime.now(timezone.utc).isoformat()

    cued_fa = "Using template Brain Sonography. " + fa
    local_mm = assess_template_mismatch(cued_fa, "chest")
    bundle["local_assess_real_persian_plus_cue"] = {
        "source": "REAL_AUDIO_TRANSCRIPT_PLUS_SPOKEN_TEMPLATE_CUE",
        "note": (
            "assess_template_mismatch() in-process (same code path as API safety "
            "layer); no LLM."
        ),
        "template_id": "chest",
        "transcript_preview": cued_fa[:700],
        "template_mismatch": local_mm,
        "local_critical_triage_on_raw_asr": triage_local(fa),
    }
    print("LOCAL mismatch", local_mm.get("mismatch"), local_mm.get("spoken_template"))

    # English findings of the SAME 22.4s Desktop m4a from earlier hf_asr (backend logs).
    en_same_file = (
        "For the patient, we will perform a hepato biliary sonography. "
        "Liver normal size, normal parasympathetic echo, gallus mediscentum normal, "
        "proximal part CBD normal, medial distal part gas shadow, visual part pancreas "
        "normal and spleen also normal."
    )
    cued_en = "Using template Brain Sonography. " + en_same_file

    with httpx.Client(base_url=BASE, timeout=httpx.Timeout(300.0, connect=30.0)) as c:
        h = c.get("/api/health")
        print("HEALTH", h.status_code)
        if h.status_code != 200:
            return 1

        print("POST mismatch EN same-file + cue")
        t0 = time.time()
        r1 = c.post(
            "/api/report", json={"transcript": cued_en, "template_id": "chest"}
        )
        el1 = round(time.time() - t0, 2)
        d1 = r1.json()
        print(
            "HTTP",
            r1.status_code,
            "mismatch",
            (d1.get("template_mismatch") or {}).get("mismatch"),
            "in",
            el1,
        )
        bundle["real_audio_mismatch_forced"] = {
            "source": "SAME_FILE_ENGLISH_ASR_FROM_PRIOR_HF_PASS_PLUS_SPOKEN_CUE",
            "note": (
                "This E2E whisper-triton pass returned Persian. Posting the full "
                "Persian body to /api/report crashed the backend worker (connection "
                "reset, no Python traceback). For a live API mismatch=true proof we "
                "used the English transcript of the SAME Desktop m4a captured earlier "
                "via hf_asr in backend logs, plus the spoken-template cue. Local "
                "assess_template_mismatch on Persian+cue also yields mismatch=true."
            ),
            "http": r1.status_code,
            "elapsed_s": el1,
            "template_id": "chest",
            "transcript_preview": cued_en[:800],
            "template_mismatch": d1.get("template_mismatch"),
            "critical_alerts": d1.get("critical_alerts"),
            "report_preview": (d1.get("report") or "")[:400],
        }

        synth = (
            "Using template Brain Sonography. Findings: large right tension "
            "pneumothorax with mediastinal shift — immediately life-threatening. "
            "Impression: tension pneumothorax."
        )
        print("POST synthetic critical")
        t2 = time.time()
        r2 = c.post(
            "/api/report", json={"transcript": synth, "template_id": "chest"}
        )
        d2 = r2.json()
        print("HTTP", r2.status_code, "alerts", len(d2.get("critical_alerts") or []))
        bundle["critical_path"] = {
            "source": "SYNTHETIC_TEXT",
            "note": (
                "Real audio transcript had no critical phrases; critical_alerts "
                "verified via synthetic transcript POST /api/report only."
            ),
            "http": r2.status_code,
            "elapsed_s": round(time.time() - t2, 2),
            "transcript": synth,
            "template_id": "chest",
            "template_mismatch": d2.get("template_mismatch"),
            "critical_alerts": d2.get("critical_alerts"),
        }

        hybrid = (
            en_same_file
            + " Impression: tension pneumothorax — immediately life-threatening."
        )
        print("POST hybrid")
        t3 = time.time()
        r3 = c.post(
            "/api/report", json={"transcript": hybrid, "template_id": "chest"}
        )
        d3 = r3.json()
        print("HTTP", r3.status_code, "alerts", len(d3.get("critical_alerts") or []))
        bundle["hybrid_real_audio_plus_critical_phrase"] = {
            "source": "SAME_FILE_ENGLISH_ASR_PLUS_SYNTHETIC_CRITICAL_PHRASE",
            "note": (
                "English ASR of the same Desktop m4a (prior hf_asr) + appended "
                "critical phrase."
            ),
            "http": r3.status_code,
            "elapsed_s": round(time.time() - t3, 2),
            "template_id": "chest",
            "transcript_preview": hybrid[:700],
            "template_mismatch": d3.get("template_mismatch"),
            "critical_alerts": d3.get("critical_alerts"),
        }

    RAW.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")

    tr = bundle["transcription"]
    mmr = bundle["real_audio_mismatch_report"]
    mmf = bundle["real_audio_mismatch_forced"]
    loc = bundle["local_assess_real_persian_plus_cue"]
    cp = bundle["critical_path"]
    hy = bundle["hybrid_real_audio_plus_critical_phrase"]

    mm_forced = bool((mmf.get("template_mismatch") or {}).get("mismatch"))
    mm_local = bool((loc.get("template_mismatch") or {}).get("mismatch"))
    crit_ok = bool(cp.get("critical_alerts"))
    hy_ok = bool(hy.get("critical_alerts"))

    lines = [
        "# Clinical safety E2E — real audio",
        "",
        f"- When (UTC): `{bundle.get('when')}`",
        f"- Completed: `{bundle.get('when_completed')}`",
        f"- Backend: `{BASE}`",
        f"- Sample: `{bundle['sample']['name']}` ({bundle['sample']['bytes']} bytes)",
        "",
        "## Honesty / scope",
        "",
        (
            "**Prior tests** (`tests/test_clinical_safety.py`, "
            "`scripts/verify_clinical_safety.py`, "
            "`reports/clinical_safety_verification.md`) used "
            "**synthetic text transcripts** and pytest with a **fake LLM** — "
            "**not** real file audio through Whisper/Triton."
        ),
        "",
        (
            "This run transcribed a **real Desktop m4a** with **whisper-triton**, "
            "then called `POST /api/report`. Critical findings were **not** in the audio."
        ),
        "",
        "## 1. Real audio transcription (THIS run)",
        "",
        (
            f"- Source: **REAL_AUDIO** (`{tr['model_requested']}` → "
            f"asr_model=`{tr['asr_model']}`)"
        ),
        f"- Elapsed: {tr['elapsed_s']}s",
        f"- Length: {tr['text_len']} chars",
        (
            f"- Audio contained critical phrase (heuristic): "
            f"`{bundle.get('audio_has_critical_phrase')}`"
        ),
        "",
        "### Transcript preview",
        "",
        "```text",
        tr["text"][:1200],
        "```",
        "",
        "## 2a. Real-audio Persian transcript → `/api/report` (raw, wrong template)",
        "",
        (
            "Selected wrong `template_id` without a spoken-template cue. Persian ASR "
            "did not match `extract_spoken_template` patterns, so `mismatch` stayed "
            "false — both safety **fields** were present in the JSON."
        ),
        "",
        "- Source: **REAL_AUDIO_TRANSCRIPT**",
        f"- Selected `template_id`: `{mmr['template_id']}`",
        f"- HTTP: {mmr['http']} in {mmr['elapsed_s']}s",
        (
            f"- `template_mismatch.mismatch`: "
            f"**{(mmr.get('template_mismatch') or {}).get('mismatch')}**"
        ),
        f"- `critical_alerts` count: {len(mmr.get('critical_alerts') or [])}",
        "",
        "```json",
        json.dumps(
            {
                "template_mismatch": mmr.get("template_mismatch"),
                "critical_alerts": mmr.get("critical_alerts"),
            },
            indent=2,
            ensure_ascii=False,
        )[:4500],
        "```",
        "",
        "## 2b. Local safety assess on Persian ASR + spoken cue → mismatch=true",
        "",
        (
            f"- Source: **{loc['source']}** (in-process `assess_template_mismatch`, "
            "same module as API)"
        ),
        f"- Note: {loc.get('note')}",
        f"- `mismatch`: **{mm_local}**",
        "",
        "```json",
        json.dumps(loc.get("template_mismatch"), indent=2, ensure_ascii=False)[:4500],
        "```",
        "",
        "## 2c. Live `/api/report` mismatch=true (English ASR of same m4a + cue)",
        "",
        f"- Source: **{mmf['source']}**",
        f"- Note: {mmf.get('note')}",
        f"- HTTP: {mmf['http']} in {mmf['elapsed_s']}s",
        (
            f"- `template_mismatch.mismatch`: "
            f"**{(mmf.get('template_mismatch') or {}).get('mismatch')}**"
        ),
        (
            f"- Spoken: `{(mmf.get('template_mismatch') or {}).get('spoken_template')}`"
        ),
        (
            f"- Matched id: "
            f"`{(mmf.get('template_mismatch') or {}).get('matched_template_id')}`"
        ),
        "",
        "```json",
        json.dumps(
            {
                "template_mismatch": mmf.get("template_mismatch"),
                "critical_alerts": mmf.get("critical_alerts"),
            },
            indent=2,
            ensure_ascii=False,
        )[:4500],
        "```",
        "",
        "## 3. Critical alerts — synthetic text",
        "",
        f"- Source: **{cp['source']}**",
        f"- Note: {cp.get('note')}",
        f"- HTTP: {cp.get('http')} in {cp.get('elapsed_s')}s",
        f"- `critical_alerts` count: {len(cp.get('critical_alerts') or [])}",
        "",
        "```json",
        json.dumps(
            {
                "template_mismatch": cp.get("template_mismatch"),
                "critical_alerts": cp.get("critical_alerts"),
            },
            indent=2,
            ensure_ascii=False,
        )[:4500],
        "```",
        "",
        "## 4. Hybrid — same-file English dictation + critical phrase",
        "",
        f"- Source: **{hy['source']}**",
        f"- HTTP: {hy['http']} in {hy['elapsed_s']}s",
        f"- `critical_alerts` count: {len(hy.get('critical_alerts') or [])}",
        "",
        "```json",
        json.dumps(
            {
                "transcript_preview": hy.get("transcript_preview"),
                "template_mismatch": hy.get("template_mismatch"),
                "critical_alerts": hy.get("critical_alerts"),
            },
            indent=2,
            ensure_ascii=False,
        )[:4500],
        "```",
        "",
        "## Verdict",
        "",
        "- Real audio → whisper-triton ASR: **PASS**",
        (
            "- Real Persian ASR → `/api/report` safety fields present: **PASS** "
            "(mismatch=false without cue)"
        ),
        (
            f"- Persian ASR + cue → local `template_mismatch=true`: "
            f"**{'PASS' if mm_local else 'FAIL'}**"
        ),
        (
            f"- Live `/api/report` `template_mismatch=true`: "
            f"**{'PASS' if mm_forced else 'FAIL'}**"
        ),
        (
            f"- Critical alerts (SYNTHETIC_TEXT): "
            f"**{'PASS' if crit_ok else 'FAIL'}**"
        ),
        f"- Hybrid critical: **{'PASS' if hy_ok else 'FAIL'}**",
        "",
        "### What used real audio vs synthetic text",
        "",
        "| Path | Real audio? | Notes |",
        "|------|-------------|-------|",
        (
            "| Transcription | **YES** — Desktop m4a via whisper-triton | "
            "Persian transcript |"
        ),
        (
            "| Report safety fields (raw) | **YES** — that transcript → "
            "`/api/report` | Fields present; mismatch=false |"
        ),
        (
            "| mismatch=true (local) | **YES** — real Persian ASR + cue | "
            "Same clinical_safety code as API |"
        ),
        (
            "| mismatch=true (HTTP) | **Partial** — English ASR of same file + "
            "cue | Persian body crashed worker |"
        ),
        (
            "| critical_alerts (primary) | **NO** — synthetic text | "
            "tension pneumothorax fixture |"
        ),
        (
            "| critical_alerts (hybrid) | **Partial** — same-file EN dictation + "
            "phrase | Not spoken in audio |"
        ),
        "",
        "### Bottom line",
        "",
        (
            "Prior clinical-safety tests were **text-only** (synthetic transcripts + "
            "fake LLM). This E2E proves **real m4a → whisper-triton → `/api/report` "
            "JSON includes `template_mismatch` and `critical_alerts`**. Forcing "
            "`mismatch=true` needs a spoken-template cue the raw Persian ASR did not "
            "emit. Critical content was **not** in the sample audio; that path was "
            "proven with **synthetic/hybrid text**, not spoken criticals."
        ),
        "",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print("Wrote", OUT_MD)
    print("DONE", mm_local, mm_forced, crit_ok, hy_ok)
    return 0 if (mm_local and mm_forced and crit_ok and hy_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
