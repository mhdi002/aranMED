#!/usr/bin/env python3
"""ASR health + functional verification.

Proves the speech pipeline is genuinely working end-to-end by sending a real
audio file through the running backend and asserting the transcript is usable.

Nothing is hardcoded:
  * backend URL      -> scripts/_endpoints.py (env: ASR_API_BASE / BACKEND_URL)
  * audio file       -> --audio, else env ASR_VERIFY_AUDIO, else auto-discovered
                        from the repo root (first audio file found)
  * model            -> --model, else the registry default from models.yaml
  * thresholds       -> --min-chars / --max-seconds (env ASR_VERIFY_MIN_CHARS,
                        ASR_VERIFY_MAX_SECONDS)

Exit code 0 = PASS, 1 = FAIL. Writes a machine-readable JSON summary so CI and
reports/ can consume it.

Usage:
    python scripts/verify_asr.py
    python scripts/verify_asr.py --audio path/to/dictation.m4a --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Transcripts and filenames are bilingual (Persian/English) and may contain
# characters the host console codepage cannot encode (e.g. cp1252 on Windows).
# Never let printing corrupt an otherwise passing verification run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from _endpoints import BACKEND_URL  # noqa: E402

AUDIO_EXTS = (".m4a", ".wav", ".mp3", ".ogg", ".webm", ".flac", ".aac")
PERSIAN_RE = re.compile(r"[؀-ۿ]")
# A transcript that is mostly punctuation/whitespace is not a real transcript.
WORD_RE = re.compile(r"[A-Za-z؀-ۿ]{2,}")


def discover_audio() -> Path | None:
    """First audio file in the repo root (env override wins). No literal name."""
    env = (os.getenv("ASR_VERIFY_AUDIO") or "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    candidates = sorted(
        (p for p in ROOT.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    return candidates[0] if candidates else None


def http_json(method: str, url: str, *, files=None, data=None, timeout=600):
    import httpx

    with httpx.Client(timeout=timeout) as c:
        r = c.request(method, url, files=files, data=data)
        try:
            return r.status_code, r.json()
        except Exception:  # noqa: BLE001
            return r.status_code, {"raw": r.text[:2000]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audio", type=Path, default=None, help="Audio file (default: auto-discover in repo root)")
    ap.add_argument("--base", default=BACKEND_URL, help=f"Backend base URL (default: {BACKEND_URL})")
    ap.add_argument("--model", default=os.getenv("ASR_VERIFY_MODEL") or None, help="ASR model name (default: registry default)")
    ap.add_argument("--language", default=os.getenv("ASR_VERIFY_LANGUAGE") or None, help="Optional language hint")
    ap.add_argument("--min-chars", type=int, default=int(os.getenv("ASR_VERIFY_MIN_CHARS", "20")))
    ap.add_argument("--max-seconds", type=float, default=float(os.getenv("ASR_VERIFY_MAX_SECONDS", "600")))
    ap.add_argument("--expect-english", action="store_true", default=(os.getenv("ASR_VERIFY_EXPECT_ENGLISH", "1").lower() not in ("0", "false", "no")),
                    help="Assert the transcript is English-normalised (pipeline default)")
    ap.add_argument("--json", action="store_true", help="Print JSON only")
    ap.add_argument("--out", type=Path, default=None, help="Write JSON summary here")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    result: dict = {"base": base, "checks": [], "ok": False}

    def check(name: str, ok: bool, detail: str = "") -> bool:
        result["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not args.json:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        return bool(ok)

    # ---- 1. backend + ASR model health -------------------------------------
    try:
        code, health = http_json("GET", f"{base}/api/health", timeout=30)
    except Exception as e:  # noqa: BLE001
        check("backend reachable", False, f"{type(e).__name__}: {e}")
        result["hint"] = f"Backend not reachable at {base}. Start the stack, then re-run."
        _emit(result, args)
        return 1

    ok_health = code == 200 and bool(health.get("ok"))
    check("backend reachable", ok_health, f"HTTP {code}")
    asr_model = health.get("asr_model")
    result["asr_model"] = asr_model
    check("ASR model configured", bool(asr_model), str(asr_model))

    # ---- 2. audio present ---------------------------------------------------
    audio = args.audio or discover_audio()
    if audio is None or not audio.is_file():
        check("audio file found", False, "no audio file in repo root; pass --audio or set ASR_VERIFY_AUDIO")
        _emit(result, args)
        return 1
    size_kb = audio.stat().st_size / 1024
    result["audio"] = {"name": audio.name, "size_kb": round(size_kb, 1)}
    check("audio file found", size_kb > 1, f"{audio.name} ({size_kb:.1f} KB)")

    # ---- 3. real transcription ---------------------------------------------
    data = {}
    if args.model:
        data["model"] = args.model
    if args.language:
        data["language"] = args.language

    t0 = time.perf_counter()
    try:
        with audio.open("rb") as fh:
            code, payload = http_json(
                "POST", f"{base}/api/transcribe",
                files={"file": (audio.name, fh, "application/octet-stream")},
                data=data or None,
                timeout=args.max_seconds,
            )
    except Exception as e:  # noqa: BLE001
        check("transcription request", False, f"{type(e).__name__}: {e}")
        _emit(result, args)
        return 1
    elapsed = time.perf_counter() - t0
    result["elapsed_sec"] = round(elapsed, 2)

    if not check("transcription request", code == 200, f"HTTP {code} in {elapsed:.1f}s"):
        result["response"] = payload
        _emit(result, args)
        return 1

    text = (payload.get("text") or "").strip()
    result["transcript_chars"] = len(text)
    result["transcript"] = text
    result["asr_model_used"] = payload.get("asr_model")

    check("transcript non-empty", len(text) >= args.min_chars, f"{len(text)} chars (min {args.min_chars})")
    words = WORD_RE.findall(text)
    result["word_count"] = len(words)
    check("transcript has real words", len(words) >= 3, f"{len(words)} word-like tokens")
    check("within time budget", elapsed <= args.max_seconds, f"{elapsed:.1f}s <= {args.max_seconds}s")

    if args.expect_english:
        has_fa = bool(PERSIAN_RE.search(text))
        check(
            "English-normalised transcript",
            not has_fa,
            "no Persian script present" if not has_fa
            else "Persian script found — check WHISPER_OUTPUT_ENGLISH / english_transcript fallback",
        )

    result["ok"] = all(c["ok"] for c in result["checks"])

    if not args.json:
        print("\n--- transcript ---")
        print(text[:1000] + ("…" if len(text) > 1000 else ""))
        print(f"\nASR model: {result.get('asr_model_used') or asr_model} | "
              f"{len(text)} chars | {elapsed:.1f}s")
        print(f"\nRESULT: {'PASS' if result['ok'] else 'FAIL'}")

    _emit(result, args)
    return 0 if result["ok"] else 1


def _emit(result: dict, args) -> None:
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    out = args.out or (ROOT / "reports" / "asr_verify.json")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    raise SystemExit(main())
