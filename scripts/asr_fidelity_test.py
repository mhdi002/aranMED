#!/usr/bin/env python3
"""End-to-end fidelity check for the two-stage ASR pipeline.

Prints the raw stage-one text beside the stage-two English and reports whether
every dictated numeral survived. This is the check that the original
verification lacked: the old pass looked at whether the English *read* well,
which a confabulation also does. See
docs/core/ASR_TRANSLATION_CONFABULATION.md.

Env: FID_BASE (gateway URL), FID_FILES (comma-separated audio paths).
Exit code is the number of files whose measurements did not survive.
"""
import json
import os
import re
import subprocess
import sys

BASE = os.environ.get("FID_BASE", "http://localhost:8090")
FILES = [f for f in os.environ.get("FID_FILES", "").split(",") if f.strip()]

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def numerals(text):
    return re.findall(r"\d+", (text or "").translate(_DIGITS))


def post(path, audio):
    out = subprocess.run(
        ["curl", "-s", "--max-time", "900", "-X", "POST", f"{BASE}{path}",
         "-F", f"file=@{audio}"],
        capture_output=True, text=True,
    ).stdout
    try:
        return json.loads(out)
    except Exception:
        return {"_raw": out[:400]}


failures = 0
for audio in FILES:
    audio = audio.strip()
    name = os.path.basename(audio)
    print("=" * 74)
    print(f"FILE: {name}")
    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", audio],
        capture_output=True, text=True,
    ).stdout.strip()
    print(f"duration: {dur}s")
    print("=" * 74)

    d = post("/api/transcribe", audio)
    if "text" not in d:
        print("ERROR:", json.dumps(d)[:400])
        failures += 1
        continue

    raw = d.get("raw_transcript") or ""
    eng = d.get("text") or ""
    degraded = d.get("translation_degraded")
    note = d.get("translation_note")

    print("\n--- STAGE 1: raw ASR (what Whisper actually heard) ---")
    print(raw or "(not recorded)")
    print("\n--- STAGE 2: English returned by the API ---")
    print(eng)

    src, out = numerals(raw), numerals(eng)
    missing = [n for n in src if out.count(n) < src.count(n)]
    invented = [n for n in out if n not in src]

    print("\n--- MEASUREMENT FIDELITY ---")
    print(f"  in raw ASR      : {src or '(none dictated)'}")
    print(f"  in English      : {out or '(none)'}")
    print(f"  dropped         : {missing or 'none'}")
    print(f"  invented        : {invented or 'none'}")
    print(f"  degraded flag   : {degraded}")
    if note:
        print(f"  note            : {note}")

    if raw and (missing or invented) and not degraded:
        print("  VERDICT         : FAIL - numbers altered and NOT flagged")
        failures += 1
    elif missing or invented:
        print("  VERDICT         : SAFE - altered, but flagged and degraded to source")
    else:
        print("  VERDICT         : PASS - every dictated number survived")
    print()

print(f"files checked: {len(FILES)}   unflagged failures: {failures}")
sys.exit(failures)
