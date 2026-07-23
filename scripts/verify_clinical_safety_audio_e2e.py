#!/usr/bin/env python3
"""Real-audio E2E for template_mismatch + critical_alerts.

Prior clinical_safety verification used synthetic TEXT transcripts only.
This script:
  1) POST /api/transcribe on a real Desktop m4a (whisper-triton preferred)
  2) POST /api/report with an intentionally wrong template_id
  3) Verifies critical_alerts via real audio if present, else synthetic/hybrid
     paths that are clearly labeled

Writes reports/clinical_safety_e2e_audio.md (+ raw JSON).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT_MD = ROOT / "reports" / "clinical_safety_e2e_audio.md"
OUT_JSON = ROOT / "reports" / "clinical_safety_e2e_audio_raw.json"
BASE = "http://127.0.0.1:8010"
DESKTOP = Path(os.environ.get("ASR_SAMPLE_DIR") or (Path.home() / "Desktop"))

CRITICAL_RE = re.compile(
    r"tension\s+pneumothorax|aortic\s+(rupture|dissection)|active\s+extravasation|"
    r"herniation|massive\s+(pe|pulmonary)|ruptured\s+ectopic|life[- ]?threatening|"
    r"critical\s+finding|pneumoperitoneum|bowel\s+(ischemia|perforation)",
    re.I,
)


def pick_wrong_template(transcript: str) -> str:
    t = (transcript or "").lower()
    if any(k in t for k in ["hepato", "biliary", "liver", "gall", "cbd"]):
        return "chest"
    if any(k in t for k in ["abdo", "pelvi", "free fluid", "freefluid"]):
        return "chest"
    if "brain" in t or "cranial" in t:
        return "chest"
    if "chest" in t or "lung" in t or "pleural" in t:
        return "brain"
    return "brain"


def main() -> int:
    files = sorted(DESKTOP.glob("Feb 3*.m4a"), key=lambda p: p.stat().st_size)
    if not files:
        print("No Feb 3*.m4a on Desktop")
        return 1
    sample = files[0]
    print("SAMPLE", sample.name, sample.stat().st_size)

    bundle: dict = {
        "when": datetime.now(timezone.utc).isoformat(),
        "sample": {
            "path": str(sample),
            "name": sample.name,
            "bytes": sample.stat().st_size,
        },
        "honesty_note": (
            "Prior clinical_safety tests used synthetic TEXT transcripts + unit "
            "tests / fake LLM paths — NOT real audio through Whisper/Triton. "
            "This run uses real m4a."
        ),
    }

    with httpx.Client(base_url=BASE, timeout=httpx.Timeout(600.0, connect=30.0)) as c:
        h = c.get("/api/health")
        bundle["health"] = {
            "status": h.status_code,
            "body": h.json() if h.status_code == 200 else h.text[:500],
        }
        print("HEALTH", h.status_code)

        asr_result = None
        audio_bytes = sample.read_bytes()
        for model in ["whisper-triton", "whisper-large-v3"]:
            print(f"Trying ASR model={model} ...")
            t0 = time.time()
            try:
                r = c.post(
                    "/api/transcribe",
                    data={"model": model},
                    files={"file": (sample.name, audio_bytes, "audio/mp4")},
                )
                elapsed = round(time.time() - t0, 2)
                print(f"  HTTP {r.status_code} in {elapsed}s")
                if r.status_code == 200:
                    body = r.json()
                    asr_result = {
                        "source": "REAL_AUDIO",
                        "model_requested": model,
                        "asr_model": body.get("asr_model"),
                        "elapsed_s": elapsed,
                        "text": body.get("text") or "",
                        "text_len": len(body.get("text") or ""),
                    }
                    print("  asr_model=", body.get("asr_model"))
                    print("  preview=", (body.get("text") or "")[:300])
                    break
                print("  fail", r.text[:300])
                bundle.setdefault("asr_errors", []).append(
                    {"model": model, "status": r.status_code, "body": r.text[:500]}
                )
            except Exception as e:  # noqa: BLE001
                elapsed = round(time.time() - t0, 2)
                print(f"  EXC after {elapsed}s: {e}")
                bundle.setdefault("asr_errors", []).append(
                    {"model": model, "exception": str(e), "elapsed_s": elapsed}
                )

        if not asr_result or not asr_result.get("text"):
            bundle["fatal"] = "ASR failed for all models"
            OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
            OUT_JSON.write_text(
                json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print("ASR failed")
            return 1

        transcript = asr_result["text"]
        bundle["transcription"] = asr_result
        wrong_tpl = pick_wrong_template(transcript)
        has_critical_in_audio = bool(CRITICAL_RE.search(transcript))
        bundle["audio_has_critical_phrase"] = has_critical_in_audio
        bundle["wrong_template_id_chosen"] = wrong_tpl

        print(f"POST /api/report wrong template={wrong_tpl} (REAL AUDIO transcript)")
        t1 = time.time()
        r_mm = c.post(
            "/api/report", json={"transcript": transcript, "template_id": wrong_tpl}
        )
        el_mm = round(time.time() - t1, 2)
        ctype = r_mm.headers.get("content-type", "")
        d_mm = (
            r_mm.json()
            if ctype.startswith("application/json")
            else {"raw": r_mm.text[:1000]}
        )
        print(f"  HTTP {r_mm.status_code} in {el_mm}s")
        mm = (d_mm.get("template_mismatch") or {}) if isinstance(d_mm, dict) else {}
        alerts_mm = (
            (d_mm.get("critical_alerts") or []) if isinstance(d_mm, dict) else []
        )
        print("  mismatch=", mm.get("mismatch"), "spoken=", mm.get("spoken_template"))
        print("  critical_alerts count=", len(alerts_mm))
        bundle["real_audio_mismatch_report"] = {
            "source": "REAL_AUDIO_TRANSCRIPT",
            "http": r_mm.status_code,
            "elapsed_s": el_mm,
            "template_id": wrong_tpl,
            "transcript_preview": transcript[:600],
            "template_mismatch": mm,
            "critical_alerts": alerts_mm,
            "report_preview": (
                (d_mm.get("report") or "")[:400] if isinstance(d_mm, dict) else None
            ),
        }

        if has_critical_in_audio and alerts_mm:
            bundle["critical_path"] = {
                "source": "REAL_AUDIO",
                "note": (
                    "Critical findings present in real audio transcript and "
                    "returned in report."
                ),
                "critical_alerts": alerts_mm,
            }
        else:
            synth = (
                "Using template Brain Sonography. Findings: large right tension "
                "pneumothorax with mediastinal shift — immediately life-threatening. "
                "Impression: tension pneumothorax."
            )
            print("POST /api/report SYNTHETIC critical+mismatch (no critical in audio)")
            t2 = time.time()
            r_c = c.post(
                "/api/report", json={"transcript": synth, "template_id": "chest"}
            )
            el_c = round(time.time() - t2, 2)
            ctype_c = r_c.headers.get("content-type", "")
            d_c = (
                r_c.json()
                if ctype_c.startswith("application/json")
                else {"raw": r_c.text[:1000]}
            )
            n_alerts = len(d_c.get("critical_alerts") or [])
            print(f"  HTTP {r_c.status_code} in {el_c}s alerts={n_alerts}")
            bundle["critical_path"] = {
                "source": "SYNTHETIC_TEXT",
                "note": (
                    "Real audio transcript did not contain critical finding phrases; "
                    "critical_alerts verified via synthetic transcript "
                    "POST /api/report only."
                ),
                "http": r_c.status_code,
                "elapsed_s": el_c,
                "transcript": synth,
                "template_id": "chest",
                "template_mismatch": d_c.get("template_mismatch"),
                "critical_alerts": d_c.get("critical_alerts"),
            }

            hybrid = (
                transcript.strip()
                + " Impression: tension pneumothorax — immediately life-threatening."
            )
            print("POST /api/report HYBRID real-audio+appended critical phrase")
            t3 = time.time()
            r_h = c.post(
                "/api/report",
                json={"transcript": hybrid, "template_id": wrong_tpl},
            )
            el_h = round(time.time() - t3, 2)
            ctype_h = r_h.headers.get("content-type", "")
            d_h = (
                r_h.json()
                if ctype_h.startswith("application/json")
                else {"raw": r_h.text[:1000]}
            )
            n_h = len(d_h.get("critical_alerts") or [])
            print(f"  HTTP {r_h.status_code} in {el_h}s alerts={n_h}")
            bundle["hybrid_real_audio_plus_critical_phrase"] = {
                "source": "REAL_AUDIO_TRANSCRIPT_PLUS_SYNTHETIC_CRITICAL_PHRASE",
                "http": r_h.status_code,
                "elapsed_s": el_h,
                "template_id": wrong_tpl,
                "transcript_preview": hybrid[:700],
                "template_mismatch": d_h.get("template_mismatch"),
                "critical_alerts": d_h.get("critical_alerts"),
            }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("JSON written", OUT_JSON)

    tr = bundle["transcription"]
    mmr = bundle["real_audio_mismatch_report"]
    cp = bundle["critical_path"]
    hy = bundle.get("hybrid_real_audio_plus_critical_phrase")

    lines: list[str] = [
        "# Clinical safety E2E — real audio",
        "",
        f"- When (UTC): `{bundle['when']}`",
        f"- Backend: `{BASE}`",
        f"- Sample: `{bundle['sample']['name']}` ({bundle['sample']['bytes']} bytes)",
        "",
        "## Honesty / scope",
        "",
        (
            "**Prior tests** (`tests/test_clinical_safety.py`, "
            "`scripts/verify_clinical_safety.py`, "
            "`reports/clinical_safety_verification.md`) used "
            "**synthetic text transcripts** and pytest/API mocks — **not** real "
            "microphone/file audio through Whisper/Triton."
        ),
        "",
        (
            "This document records a **real m4a → `/api/transcribe` → `/api/report`** "
            "verification for `template_mismatch`, plus clearly labeled critical-alert "
            "paths."
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
            f"`{bundle['audio_has_critical_phrase']}`"
        ),
        "",
        "### Transcript preview",
        "",
        "```text",
        tr["text"][:1200],
        "```",
        "",
        "## 2. Real-audio transcript → report with intentional wrong template",
        "",
        "- Source: **REAL_AUDIO_TRANSCRIPT**",
        f"- Selected (wrong) `template_id`: `{mmr['template_id']}`",
        f"- HTTP: {mmr['http']} in {mmr['elapsed_s']}s",
        (
            f"- `template_mismatch.mismatch`: "
            f"**{mmr['template_mismatch'].get('mismatch')}**"
        ),
        f"- Spoken template: `{mmr['template_mismatch'].get('spoken_template')}`",
        (
            f"- Matched template id: "
            f"`{mmr['template_mismatch'].get('matched_template_id')}`"
        ),
        (
            f"- `critical_alerts` count from this call: "
            f"{len(mmr.get('critical_alerts') or [])}"
        ),
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
        "## 3. Critical alerts path",
        "",
        f"- Source label: **{cp['source']}**",
        f"- Note: {cp.get('note')}",
    ]
    if cp.get("http") is not None:
        lines.append(f"- HTTP: {cp.get('http')} in {cp.get('elapsed_s')}s")
    lines.extend(
        [
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
    )

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

    mm_ok = bool((mmr.get("template_mismatch") or {}).get("mismatch"))
    crit_ok = bool(cp.get("critical_alerts"))
    hy_ok = bool(hy and hy.get("critical_alerts")) if hy else None

    lines.extend(
        [
            "## Verdict",
            "",
            f"- Real audio → ASR: **PASS** (`{tr['asr_model']}`)",
            (
                f"- Real audio transcript → `template_mismatch`: "
                f"**{'PASS' if mm_ok else 'FAIL'}**"
            ),
            (
                f"- Critical alerts (`{cp['source']}`): "
                f"**{'PASS' if crit_ok else 'FAIL'}**"
            ),
        ]
    )
    if hy is not None:
        lines.append(
            f"- Hybrid real+critical phrase: **{'PASS' if hy_ok else 'FAIL'}**"
        )

    lines.extend(
        [
            "",
            "### What used real audio vs synthetic text",
            "",
            "| Path | Real audio? | Endpoint |",
            "|------|-------------|----------|",
            "| Transcription | YES (Desktop m4a) | `POST /api/transcribe` |",
            (
                "| Template mismatch | YES (transcript from audio) | "
                "`POST /api/report` |"
            ),
        ]
    )
    if cp["source"] == "REAL_AUDIO":
        lines.append(
            "| Critical alerts | YES (in audio) | `POST /api/report` |"
        )
    else:
        lines.append(
            "| Critical alerts (primary) | NO — synthetic text only | "
            "`POST /api/report` |"
        )
        lines.append(
            "| Critical alerts (hybrid) | Partial — real transcript + appended "
            "phrase | `POST /api/report` |"
        )
    lines.append("")

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("MD written", OUT_MD)
    print("DONE mismatch_ok=", mm_ok, "crit_ok=", crit_ok, "hy_ok=", hy_ok)
    return 0 if (mm_ok and crit_ok and (hy_ok is None or hy_ok)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
