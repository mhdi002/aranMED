"""Show 300 ms LID timeline on first N seconds (no full transcription)."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from providers.audio_utils import decode_audio_bytes
from providers.lid_segment_asr import LIDSegmentTranscriber

# Override with ASR_TEST_AUDIO_FILE, or the first audio file found under
# ASR_TEST_AUDIO_DIR (defaults to the project root).
_test_dir = Path(os.environ.get("ASR_TEST_AUDIO_DIR", str(ROOT)))
_candidates = sorted(_test_dir.glob("*.m4a")) + sorted(_test_dir.glob("*.wav"))
TEST = Path(os.environ["ASR_TEST_AUDIO_FILE"]) if os.environ.get("ASR_TEST_AUDIO_FILE") \
    else (_candidates[0] if _candidates else _test_dir / "test-audio.m4a")
SECONDS = 40

wav, sr = decode_audio_bytes(TEST.read_bytes(), target_sr=16000, filename_hint=str(TEST))
wav = wav[: int(SECONDS * sr)]

engine = LIDSegmentTranscriber.from_config({
    "model_path": os.environ.get("VIBEVOICE_MODEL_PATH", ""),
    "binary_path": os.environ.get("CRISPASR_BINARY_PATH", "crispasr"),
    "lid_model": os.environ.get("CRISPASR_LID_MODEL_PATH", ""),
    "window_ms": 300,
    "lid_analysis_ms": 1000,
})

print(f"LID timeline for first {SECONDS}s ({len(wav)/sr:.1f}s audio)")
print("=" * 60)
for seg in engine.build_lid_timeline(wav, sr):
    print(f"  {seg.start_sec:6.2f} - {seg.end_sec:6.2f} s   {seg.language:4s}  conf={seg.confidence:.2f}")
