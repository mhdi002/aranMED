#!/usr/bin/env python3
"""Convert an OpenAI Whisper `.pt` checkpoint into the HF layout.

Why this exists: on networks that SNI-filter the Hugging Face and ModelScope
CDNs, `openaipublic.azureedge.net` is often still reachable -- it is OpenAI's
own publishing CDN and unrelated to either. But it serves the original
checkpoint format, while the Triton compat server loads through
`transformers.AutoModelForSpeechSeq2Seq`, which wants an HF directory.

transformers ships the conversion, so this is a thin, dependency-light wrapper
around it that also pulls the tokenizer/processor files the converted model
needs but the `.pt` does not contain. Those are small, and small files still
download fine on exactly the networks where the weights do not.

  python3 convert_whisper_pt_to_hf.py <checkpoint.pt> <output_dir>

Env:
  WHISPER_HF_ID   repo to source processor/tokenizer files from
                  (default openai/whisper-large-v3)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HF_ID = os.environ.get("WHISPER_HF_ID", "openai/whisper-large-v3")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    ckpt, out = Path(sys.argv[1]), Path(sys.argv[2])
    if not ckpt.is_file():
        print(f"checkpoint not found: {ckpt}")
        return 1
    out.mkdir(parents=True, exist_ok=True)

    print(f"converting {ckpt} -> {out}")
    rc = subprocess.run(
        [sys.executable, "-m",
         "transformers.models.whisper.convert_openai_to_hf",
         "--checkpoint_path", str(ckpt),
         "--pytorch_dump_folder_path", str(out)],
        capture_output=True, text=True,
    )
    if rc.returncode != 0:
        print("conversion failed:")
        print((rc.stderr or rc.stdout)[-2000:])
        return 1
    print("weights converted")

    # The checkpoint carries weights only. Without the processor and tokenizer
    # files the directory loads as a bare model and ASR fails at the feature
    # extractor, which is a confusing place to discover a missing file.
    try:
        from transformers import WhisperProcessor, WhisperTokenizerFast
        print(f"fetching processor/tokenizer from {HF_ID}")
        WhisperProcessor.from_pretrained(HF_ID).save_pretrained(str(out))
        WhisperTokenizerFast.from_pretrained(HF_ID).save_pretrained(str(out))
        print("processor + tokenizer saved")
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: could not fetch processor/tokenizer ({type(e).__name__}: {e})")
        print("         copy them from another checkout before serving.")

    # Prove the result actually loads, rather than trusting that files exist --
    # the same reason the download path verifies safetensors parse.
    try:
        from transformers import AutoModelForSpeechSeq2Seq
        AutoModelForSpeechSeq2Seq.from_pretrained(str(out))
        print("VERIFIED: model loads from converted directory")
    except Exception as e:  # noqa: BLE001
        print(f"VERIFY FAILED: {type(e).__name__}: {e}")
        return 1

    for f in sorted(out.iterdir()):
        print(f"  {f.stat().st_size:>13,}  {f.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
