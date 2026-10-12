import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from providers.audio_utils import decode_audio_bytes

# Override with ASR_TEST_AUDIO_DIR; defaults to the project root.
TEST = Path(os.environ.get("ASR_TEST_AUDIO_DIR", str(ROOT)))
for p in sorted(TEST.iterdir()):
    if p.suffix.lower() not in {".m4a", ".wav", ".mp3"}:
        continue
    w, sr = decode_audio_bytes(p.read_bytes(), target_sr=16000, filename_hint=p.name)
    print(f"{len(w)/sr:6.1f}s  {p.name}", flush=True)
