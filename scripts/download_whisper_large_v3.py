"""Download openai/whisper-large-v3 into models/whisper-large-v3 (Windows-safe)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models" / "whisper-large-v3"
MODEL_ID = "openai/whisper-large-v3"
WEIGHTS = MODEL_DIR / "model.safetensors"
EXPECTED_BYTES = 3_087_130_976
WEIGHTS_URL = f"https://huggingface.co/{MODEL_ID}/resolve/main/model.safetensors"

SMALL_FILES = [
    "config.json",
    "preprocessor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "merges.txt",
    "vocab.json",
    "generation_config.json",
    "normalizer.json",
    "added_tokens.json",
    "special_tokens_map.json",
]


def _valid_safetensors(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size != EXPECTED_BYTES:
        return False
    with path.open("rb") as f:
        head = f.read(64)
    if not head or head[:1] == b"\x00":
        return False
    try:
        from safetensors import safe_open

        with safe_open(str(path), framework="pt") as sf:
            _ = list(sf.keys())[:1]
        return True
    except Exception:
        return head.startswith((b"{", b"\x00")) and head[:1] != b"\x00"


def _download_small_files() -> None:
    from huggingface_hub import hf_hub_download

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name in SMALL_FILES:
        dest = MODEL_DIR / name
        if dest.is_file() and dest.stat().st_size > 0:
            continue
        print(f"  {name}")
        cached = hf_hub_download(
            repo_id=MODEL_ID,
            filename=name,
            local_dir=str(MODEL_DIR),
            local_dir_use_symlinks=False,
        )
        if Path(cached) != dest and Path(cached).is_file() and not dest.is_file():
            Path(cached).replace(dest)


def _download_weights_streaming_once() -> None:
    import httpx

    tmp = WEIGHTS.with_suffix(".safetensors.part")
    if WEIGHTS.exists() and not _valid_safetensors(WEIGHTS):
        WEIGHTS.unlink(missing_ok=True)

    resume_at = 0
    if tmp.is_file():
        resume_at = tmp.stat().st_size
        if resume_at > 0:
            with tmp.open("rb") as f:
                if f.read(1) == b"\x00":
                    resume_at = 0
                    try:
                        tmp.unlink()
                    except OSError:
                        tmp = WEIGHTS.with_name(
                            f"model.{int(__import__('time').time())}.safetensors.part"
                        )

    if resume_at >= EXPECTED_BYTES:
        tmp.replace(WEIGHTS)
        return

    print(f"  model.safetensors (~{EXPECTED_BYTES / 1e9:.2f} GB)")
    if resume_at:
        print(f"    resuming from {resume_at / 1e9:.2f} GB ({resume_at * 100 // EXPECTED_BYTES}%)")

    headers = {}
    if resume_at:
        headers["Range"] = f"bytes={resume_at}-"

    downloaded = resume_at
    chunk = 1024 * 1024
    mode = "ab" if resume_at else "wb"

    with httpx.stream(
        "GET",
        WEIGHTS_URL,
        follow_redirects=True,
        timeout=httpx.Timeout(60.0, read=600.0),
        headers=headers,
    ) as resp:
        if resume_at and resp.status_code == 416:
            if tmp.stat().st_size == EXPECTED_BYTES:
                tmp.replace(WEIGHTS)
                return
            tmp.unlink(missing_ok=True)
            raise RuntimeError("partial file invalid; restart download")
        if resume_at and resp.status_code not in (206, 200):
            raise RuntimeError(f"resume failed with HTTP {resp.status_code}")
        resp.raise_for_status()
        with tmp.open(mode) as out:
            for block in resp.iter_bytes(chunk):
                if not block:
                    continue
                out.write(block)
                downloaded += len(block)
                pct = downloaded * 100 // EXPECTED_BYTES
                if downloaded % (50 * chunk) < chunk:
                    print(
                        f"    {downloaded / 1e9:.2f} / {EXPECTED_BYTES / 1e9:.2f} GB ({pct}%)",
                        flush=True,
                    )

    if tmp.stat().st_size != EXPECTED_BYTES:
        raise RuntimeError(
            f"incomplete download: {tmp.stat().st_size:,} / {EXPECTED_BYTES:,} bytes"
        )

    tmp.replace(WEIGHTS)


def _download_weights_streaming(max_attempts: int = 100) -> None:
    import time

    import httpx

    retry_errors = (
        httpx.RemoteProtocolError,
        httpx.ReadTimeout,
        httpx.ConnectError,
        httpx.WriteError,
        ConnectionError,
        TimeoutError,
        RuntimeError,
    )
    for attempt in range(1, max_attempts + 1):
        try:
            _download_weights_streaming_once()
            return
        except retry_errors as exc:
            if attempt >= max_attempts:
                raise
            wait = min(30, attempt * 3)
            print(
                f"    network error: {exc}\n"
                f"    retrying in {wait}s ({attempt}/{max_attempts})...",
                flush=True,
            )
            time.sleep(wait)


def _cleanup_bad_cache() -> None:
    cache = MODEL_DIR / ".cache"
    if cache.is_dir():
        import shutil

        shutil.rmtree(cache, ignore_errors=True)


def main() -> None:
    if _valid_safetensors(WEIGHTS):
        print(f"Already ready: {WEIGHTS}")
        return

    print(f"Downloading {MODEL_ID} -> {MODEL_DIR}\n")
    _cleanup_bad_cache()
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    print("Small files:")
    _download_small_files()

    print("\nWeights (streaming, no sparse pre-allocation):")
    _download_weights_streaming()

    if not _valid_safetensors(WEIGHTS):
        print("ERROR: model.safetensors failed validation.", file=sys.stderr)
        sys.exit(1)

    print(f"\nOK: {WEIGHTS} ({WEIGHTS.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
