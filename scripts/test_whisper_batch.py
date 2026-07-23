"""Batch-test the configured Whisper ASR provider against local audio files.

Scans a directory for audio files (m4a/wav/mp3/flac/ogg/aac) and transcribes
each one through the real provider stack (``Registry`` -> ``HFASRProvider``
by default), writing all results to ``results/whisper_batch_transcripts.txt``
in the same ``===... filename ...===`` format used by earlier batch runs.

Usage:
    python scripts/test_whisper_batch.py
    python scripts/test_whisper_batch.py --dir "/path/to/audio" --model whisper-large-v3

By default it scans the project root (so dropping an .m4a/.wav next to this
repo and running the script "just works" on any machine/OS) — override with
``--dir`` or the ``ASR_TEST_AUDIO_DIR`` env var.
"""
from __future__ import annotations

import argparse
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

AUDIO_EXTS = {".m4a", ".wav", ".mp3", ".flac", ".ogg", ".aac", ".webm"}
DEFAULT_OUT = ROOT / "results" / "whisper_batch_transcripts.txt"
SEP = "=" * 72


def _default_dir() -> Path:
    return Path(os.environ.get("ASR_TEST_AUDIO_DIR", str(ROOT)))


def _find_audio_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=_default_dir(),
                        help="Directory to scan for audio files (default: project root, "
                             "or $ASR_TEST_AUDIO_DIR)")
    parser.add_argument("--model", default=None,
                        help="ASR provider name from models.yaml (default: registry default)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="Output transcript file")
    args = parser.parse_args()

    files = _find_audio_files(args.dir)
    if not files:
        print(f"No audio files ({', '.join(sorted(AUDIO_EXTS))}) found in: {args.dir}")
        sys.exit(1)

    reg = Registry.get()
    asr = await reg.get_asr(name=args.model)
    print(f"Model: {asr.model_id if hasattr(asr, 'model_id') else asr.name}  "
         f"device={getattr(asr, '_device', '?')}")
    print(f"Found {len(files)} audio file(s) in {args.dir}\n")

    blocks: list[str] = []
    for path in files:
        print(f"Transcribing: {path.name} ...")
        data = path.read_bytes()
        t0 = time.time()
        try:
            text = await asr.transcribe(data, filename_hint=path.name)
        except Exception as e:  # noqa: BLE001
            text = f"[ERROR] {type(e).__name__}: {e}"
        elapsed = time.time() - t0
        print(f"  done in {elapsed:.1f}s -> {text[:120]!r}")
        blocks.append(f"{SEP}\n{path.name}\n{SEP}\n{text}\n")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(blocks) + "\n", encoding="utf-8")
    print(f"\nSaved {len(files)} transcript(s) to: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
