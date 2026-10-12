"""Quick LID timeline + segment transcription on first N seconds."""
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from providers.lid_segment_asr import LIDSegmentTranscriber
from providers.audio_utils import decode_audio_bytes, write_temp_wav
import soundfile as sf
import tempfile

# Override with ASR_TEST_AUDIO_FILE, or the first audio file found under
# ASR_TEST_AUDIO_DIR (defaults to the project root).
_test_dir = Path(os.environ.get("ASR_TEST_AUDIO_DIR", str(ROOT)))
_candidates = sorted(_test_dir.glob("*.m4a")) + sorted(_test_dir.glob("*.wav"))
TEST = Path(os.environ["ASR_TEST_AUDIO_FILE"]) if os.environ.get("ASR_TEST_AUDIO_FILE") \
    else (_candidates[0] if _candidates else _test_dir / "test-audio.m4a")
SECONDS = 15


async def main() -> None:
    data = TEST.read_bytes()
    wav16, sr = decode_audio_bytes(data, target_sr=16000, filename_hint=str(TEST))
    wav16 = wav16[: int(SECONDS * sr)]
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        clip = f.name
    sf.write(clip, wav16, sr)
    clip_bytes = Path(clip).read_bytes()
    os.unlink(clip)

    t = LIDSegmentTranscriber.from_config({
        "model_path": os.environ.get("VIBEVOICE_MODEL_PATH", ""),
        "binary_path": os.environ.get("CRISPASR_BINARY_PATH", "crispasr"),
        "lid_model": os.environ.get("CRISPASR_LID_MODEL_PATH", ""),
    })
    t0 = time.time()
    result = await t.transcribe(clip_bytes, filename_hint=".wav")
    print(f"Done in {time.time()-t0:.1f}s\n")
    print("Timeline:")
    for s in result.timeline:
        print(f"  {s.start_sec:.1f}-{s.end_sec:.1f}s  {s.language}  conf={s.confidence:.2f}")
    print("\nTranscript:")
    print(result.text)


if __name__ == "__main__":
    asyncio.run(main())
