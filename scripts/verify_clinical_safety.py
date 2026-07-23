#!/usr/bin/env python3
"""Curl-style verification for clinical safety + template mismatch.

Uses BASE_URL from env (default http://127.0.0.1:8010). Does not hardcode
deploy hosts beyond localhost defaults for local smoke checks.

Writes reports/clinical_safety_verification.md
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = (os.getenv("BASE_URL") or os.getenv("BACKEND_URL") or "http://127.0.0.1:8010").rstrip("/")
OUT = ROOT / "reports" / "clinical_safety_verification.md"


def main() -> int:
    lines = [
        "# Clinical safety + template-mismatch verification",
        "",
        f"- When: {datetime.now(timezone.utc).isoformat()}",
        f"- BASE_URL: `{BASE}`",
        "",
    ]
    cases = []
    try:
        with httpx.Client(base_url=BASE, timeout=180.0) as c:
            # Health
            h = c.get("/api/health")
            lines.append(f"- Health: HTTP {h.status_code}")
            cases.append(("health", h.status_code == 200))

            # 1) Mismatch + critical
            transcript_crit = (
                "Using template Brain Sonography. Findings: large right tension pneumothorax "
                "with mediastinal shift — immediately life-threatening. "
                "Impression: tension pneumothorax."
            )
            r1 = c.post(
                "/api/report",
                json={"transcript": transcript_crit, "template_id": "chest"},
            )
            d1 = r1.json() if r1.status_code == 200 else {"detail": r1.text}
            ok1 = (
                r1.status_code == 200
                and bool(d1.get("critical_alerts"))
                and bool((d1.get("template_mismatch") or {}).get("mismatch"))
            )
            cases.append(("critical+mismatch", ok1))
            lines.append("## Case 1 — wrong spoken template + critical finding")
            lines.append(f"- HTTP {r1.status_code}")
            lines.append(f"- critical_alerts count: {len(d1.get('critical_alerts') or [])}")
            lines.append(
                f"- template_mismatch.mismatch: "
                f"{(d1.get('template_mismatch') or {}).get('mismatch')}"
            )
            if r1.status_code != 200:
                lines.append(f"- detail: `{str(d1.get('detail'))[:300]}`")
            lines.append("```json")
            lines.append(
                json.dumps(
                    {
                        "critical_alerts": d1.get("critical_alerts"),
                        "template_mismatch": d1.get("template_mismatch"),
                    },
                    indent=2,
                    ensure_ascii=False,
                )[:4000]
            )
            lines.append("```")
            lines.append("")

            # 2) Non-critical matched
            transcript_ok = (
                "This is a chest sonography. No pleural effusion. No pneumothorax. "
                "Diaphragmatic movement is normal. Impression: no acute process."
            )
            r2 = c.post(
                "/api/report",
                json={"transcript": transcript_ok, "template_id": "chest"},
            )
            d2 = r2.json() if r2.status_code == 200 else {"detail": r2.text}
            ok2 = (
                r2.status_code == 200
                and (d2.get("critical_alerts") or []) == []
                and (d2.get("template_mismatch") or {}).get("mismatch") is False
            )
            cases.append(("non-critical matched", ok2))
            lines.append("## Case 2 — non-critical, matching template")
            lines.append(f"- HTTP {r2.status_code}")
            lines.append(f"- critical_alerts: {d2.get('critical_alerts')}")
            lines.append(
                f"- template_mismatch.mismatch: "
                f"{(d2.get('template_mismatch') or {}).get('mismatch')}"
            )
            if r2.status_code != 200:
                lines.append(f"- detail: `{str(d2.get('detail'))[:300]}`")
            lines.append("")

    except Exception as e:  # noqa: BLE001
        lines.append(f"**ERROR contacting backend:** `{e}`")
        lines.append("")
        lines.append(
            "Backend may be down. Unit tests in "
            "`tests/test_clinical_safety.py` still validate local triage."
        )
        cases.append(("connectivity", False))

    passed = sum(1 for _, ok in cases if ok)
    lines.append("## Summary")
    for name, ok in cases:
        lines.append(f"- {'PASS' if ok else 'FAIL'}: {name}")
    lines.append(f"- Result: **{passed}/{len(cases)} passed**")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(OUT.read_text(encoding="utf-8"))
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
