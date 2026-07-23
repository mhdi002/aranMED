"""Compare VAD code-switch vs 300 ms LID segmentation on one file."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from registry import Registry  # noqa: E402

# Override with ASR_TEST_AUDIO_FILE, or the first audio file found under
# ASR_TEST_AUDIO_DIR (defaults to the project root).
_test_dir = Path(os.environ.get("ASR_TEST_AUDIO_DIR", str(ROOT)))
_candidates = sorted(_test_dir.glob("*.m4a")) + sorted(_test_dir.glob("*.wav"))
TEST = Path(os.environ["ASR_TEST_AUDIO_FILE"]) if os.environ.get("ASR_TEST_AUDIO_FILE") \
    else (_candidates[0] if _candidates else _test_dir / "test-audio.m4a")
BASELINE = "vibevoice-codeswitch"


async def run_one(name: str) -> dict:
    reg = Registry.get()
    asr = await reg.get_asr(name=name)
    data = TEST.read_bytes()
    t0 = time.time()
    # Skip LLM English step for fair ASR comparison
    if hasattr(asr, "output_english"):
        asr.output_english = False
    text = await asr.transcribe(data)
    elapsed = time.time() - t0
    timeline = getattr(asr, "last_timeline", None)
    return {"name": name, "seconds": round(elapsed, 1), "text": text, "timeline": timeline}


async def main() -> None:
    if not TEST.exists():
        print("Missing test file:", TEST)
        sys.exit(1)
    print("File:", TEST.name, "\n")
    for name in (BASELINE, "vibevoice-lid-segment"):
        print("=" * 72)
        print(name)
        try:
            r = await run_one(name)
            print(f"time: {r['seconds']}s")
            if r["timeline"]:
                print("timeline (first 10 segments):")
                for seg in r["timeline"][:10]:
                    print(f"  {seg['start']:.1f}-{seg['end']:.1f}s  {seg['language']}  p={seg['confidence']}")
            print(r["text"])
        except Exception as exc:
            print("ERROR:", exc)
        print()


if __name__ == "__main__":
    asyncio.run(main())
