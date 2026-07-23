"""Test openai/whisper-large-v3 on one radiology voice file."""
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

# Override with ASR_TEST_AUDIO_DIR; defaults to the project root so this
# script works out of the box on any machine/OS.
TEST_DIR = Path(os.environ.get("ASR_TEST_AUDIO_DIR", str(ROOT)))
OUT = ROOT / "results" / "whisper_large_v3_output.txt"


def _pick_test_file() -> Path:
    if TEST_DIR.is_dir():
        for ext in (".m4a", ".wav", ".mp3"):
            files = sorted(TEST_DIR.glob(f"*{ext}"))
            if files:
                return files[0]
    return TEST_DIR / "test-audio.m4a"


async def main() -> None:
    test = _pick_test_file()
    if not test.is_file():
        print("Missing test audio in:", TEST_DIR)
        sys.exit(1)

    reg = Registry.get()
    asr = await reg.get_asr(name="whisper-large-v3")
    print(f"Model: {asr.model_id}  device={getattr(asr, '_device', '?')}")
    print(f"output_english={asr.output_english}  force_language={asr.force_language}")
    print(f"File: {test.name}\n")

    data = test.read_bytes()
    t0 = time.time()
    text = await asr.transcribe(data, filename_hint=test.name)
    elapsed = time.time() - t0

    raw = getattr(asr, "last_raw_transcript", "")
    lines = [
        f"File: {test.name}",
        f"Time: {elapsed:.1f}s",
        f"output_english: {asr.output_english}",
        "",
        "=== TRANSCRIPT ===",
        text,
    ]
    if raw and raw != text:
        lines.extend(["", "=== RAW (before translate) ===", raw])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nSaved: {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
