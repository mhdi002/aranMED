"""Batch-transcribe radiology dictations for Persian–English code-switching QA."""
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

# Override with ASR_TEST_AUDIO_DIR, or pass --dir; defaults to the project root
# so this script works out of the box on any machine/OS.
TEST_DIR = Path(os.environ.get("ASR_TEST_AUDIO_DIR", str(ROOT)))
AUDIO_EXTS = {".m4a", ".wav", ".mp3", ".flac", ".ogg", ".aac"}


async def transcribe_file(asr, path: Path) -> tuple[str, str | None]:
    data = path.read_bytes()
    t0 = time.time()
    text = await asr.transcribe(data, language=None, filename_hint=path.name)
    raw = getattr(asr, "last_raw_transcript", None)
    elapsed = time.time() - t0
    print(f"  time: {elapsed:.1f}s")
    return text, raw


async def main() -> None:
    if not TEST_DIR.is_dir():
        print(f"Test folder not found: {TEST_DIR}")
        sys.exit(1)

    files = sorted(
        p for p in TEST_DIR.iterdir() if p.suffix.lower() in AUDIO_EXTS
    )
    if not files:
        print(f"No audio files in {TEST_DIR}")
        sys.exit(1)

    registry = Registry.get()
    asr = await registry.get_asr()
    print(f"ASR provider: {asr.name} [{asr.kind}]")
    print(f"force_language: {getattr(asr, 'force_language', 'n/a')}")
    print(f"output_english: {getattr(asr, 'output_english', 'n/a')}")
    print(f"Files: {len(files)}\n")

    results: list[tuple[str, str, str | None]] = []
    for i, path in enumerate(files, 1):
        print("=" * 72)
        print(f"[{i}/{len(files)}] {path.name}")
        try:
            text, raw = await transcribe_file(asr, path)
            results.append((path.name, text, raw))
            if raw and raw != text:
                print("[raw bilingual ASR]")
                print(raw)
                print("\n[English dictation]")
            print(text)
        except Exception as exc:
            print(f"ERROR: {exc}")
            results.append((path.name, f"ERROR: {exc}", None))

    out = ROOT / "results" / "whisper_batch_transcripts.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for name, text, raw in results:
            f.write(f"{'=' * 72}\n{name}\n{'=' * 72}\n")
            if raw and raw != text:
                f.write(f"[RAW]\n{raw}\n\n[ENGLISH]\n")
            f.write(f"{text}\n\n")
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    asyncio.run(main())
