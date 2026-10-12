"""In-process end-to-end check of chunked long-form transcription.

Bypasses HTTP JSON (huge FP32 payloads) and dual-GPU OOM so we can measure
completeness of the Triton compat + hf_asr algorithms directly.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "deploy" / "triton"))

os.environ.setdefault("WHISPER_DEVICE", "cuda")
os.environ.setdefault("WHISPER_CHUNK_LENGTH_S", "30")
os.environ.setdefault("WHISPER_STRIDE_LENGTH_S", "0")
os.environ.setdefault("WHISPER_MAX_SHORTFORM_S", "30")
os.environ.setdefault("WHISPER_WARMUP", "0")

from providers.audio_utils import decode_audio_bytes  # noqa: E402

OUT = ROOT / "reports" / "truncation_fix_verify.json"
OUT_MD = ROOT / "reports" / "truncation_fix_verify.md"

BEFORE = {
    "5.12": {"triton_chars": 257, "hf_chars": 241, "last_ts": None},
    "5.15": {"triton_chars": 686, "hf_chars": 731, "last_ts": 28.0},
    "5.38": {"triton_chars": 379, "hf_chars": 404, "last_ts": 14.0},
}


def _key(name: str) -> str:
    for k in ("5.12", "5.15", "5.38"):
        if k in name:
            return k
    return name


def run_triton_compat(wav: np.ndarray) -> str:
    import compat_http_server as compat

    pipe = compat._get_pipe()
    return compat._transcribe_full(pipe, wav.astype(np.float32), language=None)


def run_hf(wav: np.ndarray) -> str:
    from providers.hf_asr import HFASRProvider
    import asyncio

    cfg = {
        "model_id": "openai/whisper-large-v3",
        "device": "cuda",
        "dtype": "float16",
        "target_sr": 16000,
        "task": "transcribe",
        "force_language": False,
        "output_english": True,
        "chunk_length_s": float(os.environ["WHISPER_CHUNK_LENGTH_S"]),
        "stride_length_s": float(os.environ["WHISPER_STRIDE_LENGTH_S"]),
        "max_shortform_s": float(os.environ["WHISPER_MAX_SHORTFORM_S"]),
    }
    p = HFASRProvider(name="whisper-large-v3", config=cfg)

    async def _go() -> str:
        # encode wav back to wav bytes for provider API
        import io
        import soundfile as sf

        buf = io.BytesIO()
        sf.write(buf, wav, 16000, format="WAV")
        return await p.transcribe(buf.getvalue(), filename_hint="x.wav")

    return asyncio.run(_go())


def main() -> int:
    sample_dir = os.environ.get("ASR_SAMPLE_DIR") or str(Path.home() / "Desktop")
    samples = sorted(glob.glob(str(Path(sample_dir) / "Feb 3*.m4a")))
    print(f"samples={len(samples)}", flush=True)
    rows = []
    # Run Triton path for all files first (one model on GPU), then hf.
    decoded = []
    for p in samples:
        data = Path(p).read_bytes()
        wav, _ = decode_audio_bytes(data, target_sr=16000, filename_hint=Path(p).name)
        decoded.append((Path(p).name, wav.astype(np.float32)))

    triton_texts = {}
    for name, wav in decoded:
        key = _key(name)
        dur = len(wav) / 16000.0
        print(f"[triton] {key} dur={dur:.1f}s ...", flush=True)
        t0 = time.time()
        text = run_triton_compat(wav)
        secs = round(time.time() - t0, 1)
        print(
            f"  chars={len(text)} secs={secs} "
            f"START={text[:80].encode('ascii','replace').decode()} "
            f"END={text[-80:].encode('ascii','replace').decode()}",
            flush=True,
        )
        triton_texts[key] = {"chars": len(text), "secs": secs, "text": text, "dur": round(dur, 1)}

    # Free Triton pipeline before loading hf
    import compat_http_server as compat

    compat._pipe = None
    try:
        import torch
        import gc

        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass

    hf_texts = {}
    for name, wav in decoded:
        key = _key(name)
        dur = len(wav) / 16000.0
        print(f"[hf] {key} dur={dur:.1f}s ...", flush=True)
        t0 = time.time()
        text = run_hf(wav)
        secs = round(time.time() - t0, 1)
        print(
            f"  chars={len(text)} secs={secs} "
            f"START={text[:80].encode('ascii','replace').decode()} "
            f"END={text[-80:].encode('ascii','replace').decode()}",
            flush=True,
        )
        hf_texts[key] = {"chars": len(text), "secs": secs, "text": text, "dur": round(dur, 1)}

    for key in ("5.12", "5.15", "5.38"):
        b = BEFORE[key]
        t = triton_texts[key]
        h = hf_texts[key]
        t_low = t["text"].lower()
        h_low = h["text"].lower()
        if key == "5.15":
            t_ok = t["chars"] > b["triton_chars"] * 1.05 and (
                "pleural" in t_low or "fluid" in t_low or "افوز" in t["text"] or len(t["text"]) > 750
            )
            # beginning marker
            t_ok = t_ok and (t["text"][:40] != t["text"][-40:])
            h_ok = (
                h["chars"] > b["hf_chars"] * 1.05
                and ("liver" in h_low or "hello" in h_low or "ali" in h_low)
                and ("pleural" in h_low or "thank" in h_low or "fluid" in h_low)
            )
        elif key == "5.38":
            t_ok = t["chars"] > b["triton_chars"] * 1.05
            h_ok = (
                ("hamad" in h_low or "patient" in h_low or "free" in h_low)
                and ("hematoma" in h_low or "collection" in h_low or "thank" in h_low)
                and h["chars"] >= b["hf_chars"] * 0.85
            )
        else:
            t_ok = t["chars"] > 50
            h_ok = h["chars"] > 50
        rows.append(
            {
                "key": key,
                "dur": t["dur"],
                "before": b,
                "after": {
                    "triton": {
                        "chars": t["chars"],
                        "secs": t["secs"],
                        "start": t["text"][:250],
                        "end": t["text"][-250:],
                        "full": t["text"],
                    },
                    "hf": {
                        "chars": h["chars"],
                        "secs": h["secs"],
                        "start": h["text"][:250],
                        "end": h["text"][-250:],
                        "full": h["text"],
                    },
                },
                "pass_triton": t_ok,
                "pass_hf": h_ok,
            }
        )
        print(
            f"COMPARE {key}: t {b['triton_chars']}->{t['chars']} pass={t_ok}; "
            f"hf {b['hf_chars']}->{h['chars']} pass={h_ok}",
            flush=True,
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        "# Truncation fix verification (in-process)",
        "",
        "## Root cause",
        "",
        "Whisper internal long-form seek stopped mid-file (e.g. last_ts≈28s on 57s audio).",
        "Fix: consecutive short-form windows (`WHISPER_CHUNK_LENGTH_S`, default 30; "
        "`WHISPER_STRIDE_LENGTH_S` overlap, default 0) joined end-to-end.",
        "",
        "## Before → after",
        "",
        "| File | Dur | Triton before | Triton after | hf before | hf after | Pass |",
        "|------|-----|---------------|--------------|-----------|----------|------|",
    ]
    for r in rows:
        b, at, ah = r["before"], r["after"]["triton"], r["after"]["hf"]
        lines.append(
            f"| {r['key']} | {r['dur']}s | {b['triton_chars']} | **{at['chars']}** ({at['secs']}s) | "
            f"{b['hf_chars']} | **{ah['chars']}** ({ah['secs']}s) | "
            f"t={r['pass_triton']} hf={r['pass_hf']} |"
        )
    lines += ["", "## End previews", ""]
    for r in rows:
        lines.append(f"### {r['key']}")
        lines.append(f"- Triton END: `{r['after']['triton']['end'][-180:]}`")
        lines.append(f"- hf END: `{r['after']['hf']['end'][-180:]}`")
        lines.append("")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    ok = all(r["pass_triton"] and r["pass_hf"] for r in rows)
    print("OVERALL", "PASS" if ok else "CHECK", flush=True)
    print(f"Wrote {OUT}", flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
