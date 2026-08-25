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

# Numbers are dictated as digits AND as words, and a faithful translation
# renders both as digits. Comparing digits alone marks a correct rendering of
# "سی" as "30" an invented number -- the same false positive the backend
# guard already resolves, which this check has to mirror or it contradicts it.
_NUM_WORDS = {
    "یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "هفت": 7,
    "هشت": 8, "ده": 10, "بیست": 20, "سی": 30, "چهل": 40, "پنجاه": 50,
    "شصت": 60, "هفتاد": 70, "هشتاد": 80, "نود": 90, "صد": 100,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100,
}


def numerals(text):
    return re.findall(r"\d+", (text or "").translate(_DIGITS))


def word_numerals(text):
    out = []
    for tok in re.findall(r"[^\W\d_]+", text or "", flags=re.UNICODE):
        v = _NUM_WORDS.get(tok.lower())
        if v is not None:
            out.append(str(v))
    return out


def source_numerals(text):
    return numerals(text) + word_numerals(text)


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

    src = numerals(raw)                 # digits the source states literally
    allowed = source_numerals(raw)      # ...plus the ones it states as words
    out = numerals(eng) + word_numerals(eng)
    missing = [n for n in src if out.count(n) < src.count(n)]
    invented = [n for n in out if n not in allowed]

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
        print("  VERDICT         : SAFE - altered, but flagged to the caller")
    else:
        print("  VERDICT         : PASS - every dictated number survived")

    # The transcript is only half the question. A measurement that survives ASR
    # and then vanishes from the generated report is still lost to the
    # clinician, so follow it all the way through /api/dictate.
    print()
    print("--- STAGE 3: generated report ---")
    d2 = post("/api/dictate", audio)
    report = d2.get("report") or ""
    if not report:
        print("  (no report returned:", json.dumps(d2)[:200], ")")
    else:
        print(f"  template: {d2.get('template_id')}   model: {d2.get('model')}")
        print(f"  language: {'PERSIAN (unusable downstream)' if any('؀' <= c <= 'ۿ' for c in report) else 'English'}")
        print()
        print(report)
        rep_nums = numerals(report)
        lost = [n for n in src if n not in rep_nums]
        print()
        print(f"  numbers in raw ASR : {src or '(none dictated)'}")
        print(f"  numbers in report  : {rep_nums or '(none)'}")
        if src and lost:
            print(f"  MISSING FROM REPORT: {lost}")
            failures += 1
        elif src:
            print("  REPORT VERDICT     : PASS - dictated measurements reached the report")
        else:
            print("  REPORT VERDICT     : n/a - nothing measured in this dictation")
    print()

print(f"files checked: {len(FILES)}   unflagged failures: {failures}")
sys.exit(failures)
