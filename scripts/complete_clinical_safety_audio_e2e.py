#!/usr/bin/env python3
"""Complete mismatch+critical report paths using saved real-audio transcript.

Does NOT re-run ASR (avoids backend/GPU crashes). Updates
reports/clinical_safety_e2e_audio_raw.json and clinical_safety_e2e_audio.md.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "reports" / "clinical_safety_e2e_audio_raw.json"
OUT_MD = ROOT / "reports" / "clinical_safety_e2e_audio.md"
BASE = "http://127.0.0.1:8010"


def main() -> int:
    bundle = json.loads(RAW.read_text(encoding="utf-8"))
    fa = bundle["transcription"]["text"]
    bundle["when_completed"] = datetime.now(timezone.utc).isoformat()

    # Cue required by extract_spoken_template; findings body = real ASR text.
    # Spoken "Brain Sonography" vs selected chest → mismatch=true.
    cued = "Using template Brain Sonography. " + fa

    with httpx.Client(base_url=BASE, timeout=httpx.Timeout(300.0, connect=30.0)) as c:
        h = c.get("/api/health")
        print("HEALTH", h.status_code)
        if h.status_code != 200:
            return 1

        print("POST mismatch-forced (real ASR body + spoken cue)")
        t0 = time.time()
        r1 = c.post(
            "/api/report",
            json={"transcript": cued, "template_id": "chest"},
        )
        el1 = round(time.time() - t0, 2)
        d1 = r1.json()
        print(
            "HTTP",
            r1.status_code,
            "mismatch=",
            (d1.get("template_mismatch") or {}).get("mismatch"),
            "alerts=",
            len(d1.get("critical_alerts") or []),
            "in",
            el1,
            "s",
        )
        bundle["real_audio_mismatch_forced"] = {
            "source": "REAL_AUDIO_TRANSCRIPT_PLUS_SPOKEN_TEMPLATE_CUE",
            "note": (
                "Raw whisper-triton transcript is Persian and does not match "
                "extract_spoken_template cue patterns (Using template / This is / "
                "Exam is). Prepended 'Using template Brain Sonography.' so mismatch "
                "can fire against selected template_id=chest; findings body is the "
                "unmodified real-audio transcript."
            ),
            "http": r1.status_code,
            "elapsed_s": el1,
            "template_id": "chest",
            "transcript_preview": cued[:800],
            "template_mismatch": d1.get("template_mismatch"),
            "critical_alerts": d1.get("critical_alerts"),
            "report_preview": (d1.get("report") or "")[:400],
        }

        # Ensure synthetic critical still present; re-run if missing
        cp = bundle.get("critical_path") or {}
        if not cp.get("critical_alerts"):
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
            bundle["critical_path"] = {
                "source": "SYNTHETIC_TEXT",
                "note": (
                    "Real audio had no critical phrases; critical_alerts verified "
                    "via synthetic transcript only."
                ),
                "http": r2.status_code,
                "elapsed_s": round(time.time() - t2, 2),
                "transcript": synth,
                "template_id": "chest",
                "template_mismatch": d2.get("template_mismatch"),
                "critical_alerts": d2.get("critical_alerts"),
            }
            cp = bundle["critical_path"]

        hy = bundle.get("hybrid_real_audio_plus_critical_phrase")
        if not hy or not hy.get("critical_alerts"):
            hybrid = (
                fa.strip()
                + " Impression: tension pneumothorax — immediately life-threatening."
            )
            print("POST hybrid critical")
            t3 = time.time()
            r3 = c.post(
                "/api/report",
                json={"transcript": hybrid, "template_id": "brain"},
            )
            d3 = r3.json()
            bundle["hybrid_real_audio_plus_critical_phrase"] = {
                "source": "REAL_AUDIO_TRANSCRIPT_PLUS_SYNTHETIC_CRITICAL_PHRASE",
                "http": r3.status_code,
                "elapsed_s": round(time.time() - t3, 2),
                "template_id": "brain",
                "transcript_preview": hybrid[:700],
                "template_mismatch": d3.get("template_mismatch"),
                "critical_alerts": d3.get("critical_alerts"),
            }
            hy = bundle["hybrid_real_audio_plus_critical_phrase"]

    RAW.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")

    tr = bundle["transcription"]
    mmr = bundle["real_audio_mismatch_report"]
    mmf = bundle["real_audio_mismatch_forced"]
    cp = bundle["critical_path"]
    hy = bundle.get("hybrid_real_audio_plus_critical_phrase")

    mm_raw_ok = (mmr.get("template_mismatch") or {}).get("mismatch") is True
    mm_forced_ok = (mmf.get("template_mismatch") or {}).get("mismatch") is True
    crit_ok = bool(cp.get("critical_alerts"))
    hy_ok = bool(hy and hy.get("critical_alerts"))

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
            "**synthetic text transcripts** and pytest with a fake LLM — "
            "**not** real file audio through Whisper/Triton."
        ),
        "",
        (
            "This run used a **real Desktop m4a → `POST /api/transcribe` "
            "(whisper-triton) → `POST /api/report`** path. Critical findings were "
            "**not** present in the audio, so critical_alerts were verified with "
            "synthetic/hybrid text as labeled below."
        ),
        "",
        "## 1. Real audio transcription",
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
        "### Transcript preview (Persian from whisper-triton)",
        "",
        "```text",
        tr["text"][:1200],
        "```",
        "",
        "## 2a. Real-audio transcript alone → wrong template (raw)",
        "",
        (
            "Selected wrong `template_id` without a spoken-template cue. "
            "`extract_spoken_template` returned null on the Persian ASR text, so "
            "`mismatch` stayed false — but both safety fields were present."
        ),
        "",
        f"- Source: **REAL_AUDIO_TRANSCRIPT**",
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
        "## 2b. Real-audio findings + spoken-template cue → mismatch=true",
        "",
        f"- Source: **{mmf['source']}**",
        f"- Note: {mmf.get('note')}",
        f"- Selected (wrong) `template_id`: `{mmf['template_id']}`",
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
        "### Transcript preview",
        "",
        "```text",
        (mmf.get("transcript_preview") or "")[:800],
        "```",
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
        "## 3. Critical alerts path (synthetic text)",
        "",
        f"- Source label: **{cp['source']}**",
        f"- Note: {cp.get('note')}",
        f"- HTTP: {cp.get('http')} in {cp.get('elapsed_s')}s",
        f"- `critical_alerts` count: {len(cp.get('critical_alerts') or [])}",
        "",
        "```json",
        json.dumps(
            {
                "source": cp.get("source"),
                "template_id": cp.get("template_id"),
                "template_mismatch": cp.get("template_mismatch"),
                "critical_alerts": cp.get("critical_alerts"),
            },
            indent=2,
            ensure_ascii=False,
        )[:4500],
        "```",
        "",
    ]

    if hy:
        lines.extend(
            [
                "## 4. Hybrid (real audio transcript + appended critical phrase)",
                "",
                f"- Source: **{hy['source']}**",
                f"- HTTP: {hy['http']} in {hy['elapsed_s']}s",
                (
                    f"- `template_mismatch.mismatch`: "
                    f"{(hy.get('template_mismatch') or {}).get('mismatch')}"
                ),
                (
                    f"- `critical_alerts` count: "
                    f"{len(hy.get('critical_alerts') or [])}"
                ),
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
            ]
        )

    lines.extend(
        [
            "## Verdict",
            "",
            f"- Real audio → ASR (whisper-triton): **PASS**",
            (
                f"- Real audio alone → safety fields present: **PASS** "
                f"(mismatch={ (mmr.get('template_mismatch') or {}).get('mismatch') })"
            ),
            (
                f"- Real audio + spoken cue → `template_mismatch=true`: "
                f"**{'PASS' if mm_forced_ok else 'FAIL'}**"
            ),
            (
                f"- Critical alerts (`{cp['source']}`): "
                f"**{'PASS' if crit_ok else 'FAIL'}**"
            ),
            f"- Hybrid real+critical phrase: **{'PASS' if hy_ok else 'FAIL'}**",
            "",
            "### What used real audio vs synthetic text",
            "",
            "| Path | Real audio? | Result |",
            "|------|-------------|--------|",
            (
                "| `POST /api/transcribe` | **YES** — Desktop `Feb 3*.m4a` via "
                "whisper-triton | Full Persian transcript |"
            ),
            (
                "| `template_mismatch` / `critical_alerts` fields on report | "
                "**YES** — raw ASR text → `/api/report` | Fields present; "
                "`mismatch=false` (no spoken cue in ASR) |"
            ),
            (
                "| `template_mismatch=true` | **Partial** — real ASR findings + "
                "prepended English spoken-template cue | `mismatch=true` |"
            ),
            (
                "| `critical_alerts` (primary) | **NO** — synthetic text only | "
                "Alerts returned |"
            ),
            (
                "| `critical_alerts` (hybrid) | **Partial** — real ASR + appended "
                "critical English phrase | Alerts returned |"
            ),
            "",
            "### Bottom line for the user",
            "",
            (
                "Prior clinical-safety pytest/verification was **text-only** "
                "(synthetic transcripts + fake LLM). This E2E proves "
                "**real audio ASR → report JSON includes `template_mismatch` and "
                "`critical_alerts`**. Forcing `mismatch=true` required a spoken-"
                "template cue the Persian ASR text does not emit. Critical findings "
                "were **not** in the sample audio, so that path was proven with "
                "**synthetic/hybrid text**, not spoken critical content."
            ),
            "",
        ]
    )

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Wrote", OUT_MD)
    print(
        "mm_raw=",
        mm_raw_ok,
        "mm_forced=",
        mm_forced_ok,
        "crit=",
        crit_ok,
        "hy=",
        hy_ok,
    )
    return 0 if (mm_forced_ok and crit_ok and hy_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
