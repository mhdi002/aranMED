"""Pre-flight checks before going live on a server.

Usage:
    PYTHONPATH=backend python scripts/verify_deploy.py
    PYTHONPATH=backend python scripts/verify_deploy.py --full   # load Whisper (GPU, slow)
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
MODELS_YAML = BACKEND / "models.yaml"

FAILURES: list[str] = []
WARNINGS: list[str] = []


def ok(msg: str) -> None:
    print(f"  OK  {msg}")


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"  FAIL  {msg}")


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  WARN  {msg}")


def check_python() -> None:
    print("[Python]")
    if sys.version_info < (3, 10):
        fail(f"Python 3.10+ required (found {sys.version_info.major}.{sys.version_info.minor})")
    else:
        ok(f"version {sys.version_info.major}.{sys.version_info.minor}")


def check_imports() -> None:
    print("[Python packages]")
    required = [
        "torch",
        "torchaudio",
        "transformers",
        "fastapi",
        "uvicorn",
        "soundfile",
        "librosa",
        "numpy",
        "httpx",
        "yaml",
        "anyio",
    ]
    for name in required:
        try:
            __import__(name)
            ok(name)
        except ImportError:
            fail(f"missing package: {name} (run make install)")


def check_torch_cuda() -> None:
    print("[GPU]")
    try:
        import torch

        ok(f"torch {torch.__version__}")
        if torch.cuda.is_available():
            ok(f"CUDA available — {torch.cuda.get_device_name(0)}")
        else:
            warn("CUDA not available — Whisper will be very slow on CPU")
    except Exception as exc:  # noqa: BLE001
        fail(f"torch check failed: {exc}")


def check_ffmpeg() -> None:
    print("[ffmpeg]")
    if shutil.which("ffmpeg"):
        ok("ffmpeg on PATH")
    else:
        fail("ffmpeg not found — required for m4a/mp3 uploads")


def check_whisper_cache() -> None:
    print("[Whisper large-v3]")
    hf_home = Path.home() / ".cache" / "huggingface" / "hub"
    repo_dir = hf_home / "models--openai--whisper-large-v3"
    if repo_dir.is_dir():
        snaps = list((repo_dir / "snapshots").glob("*/model.safetensors"))
        if snaps:
            ok(f"cached at {snaps[0].parent}")
            return
    try:
        from huggingface_hub import snapshot_download

        path = snapshot_download(
            "openai/whisper-large-v3",
            allow_patterns=[
                "config.json",
                "preprocessor_config.json",
                "model.safetensors",
                "tokenizer.json",
                "generation_config.json",
            ],
        )
        ok(f"hub snapshot ready at {path}")
    except Exception as exc:  # noqa: BLE001
        fail(f"Whisper model not cached and download failed: {exc}")


def check_ollama() -> None:
    print("[Ollama]")
    import os

    import httpx
    import yaml

    host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    model_name = os.getenv("OLLAMA_MODEL", "")
    if not model_name and MODELS_YAML.is_file():
        data = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8")) or {}
        for entry in data.get("models", []):
            if entry.get("role") == "core" and entry.get("provider") == "ollama":
                cfg = entry.get("config") or {}
                model_name = cfg.get("model") or entry.get("name", "")
                break

    try:
        resp = httpx.get(f"{host}/api/tags", timeout=5.0)
        resp.raise_for_status()
        names = [m.get("name", "") for m in resp.json().get("models", [])]
        ok(f"reachable at {host} ({len(names)} models)")
        if model_name:
            found = any(model_name in n or n.startswith(model_name) for n in names)
            if found:
                ok(f"LLM model present: {model_name}")
            else:
                fail(f"LLM model missing: {model_name} (run make install or ollama pull)")
    except Exception as exc:  # noqa: BLE001
        fail(f"Ollama not reachable at {host}: {exc}")


def check_optional_paths() -> None:
    print("[Optional local models]")
    if not MODELS_YAML.is_file():
        warn("backend/models.yaml not found")
        return
    import yaml

    data = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8")) or {}
    for entry in data.get("models", []):
        if not entry.get("default"):
            continue
        cfg = entry.get("config") or {}
        for key in ("model_path", "mmproj_path", "binary_path"):
            p = cfg.get(key)
            if not p:
                continue
            path = Path(str(p))
            if path.is_file():
                ok(f"{entry.get('name')}: {key}")
            else:
                warn(f"{entry.get('name')}: {key} not found at {p} (disable in models.yaml if unused)")


def check_asr_load(full: bool) -> None:
    if not full:
        print("[Whisper load test]")
        warn("skipped (pass --full to load model on GPU)")
        return
    print("[Whisper load test]")
    sys.path.insert(0, str(BACKEND))
    try:
        import asyncio

        from registry import Registry

        async def _load() -> None:
            reg = Registry.get()
            asr = await reg.get_asr(name="whisper-large-v3")
            health = await asr.health()
            ok(f"loaded {asr.name} on {health.get('device', '?')}")
            await reg.unload_all()

        asyncio.run(_load())
    except Exception as exc:  # noqa: BLE001
        fail(f"Whisper failed to load: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify server deployment readiness")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Load Whisper on GPU (slow, confirms ASR works end-to-end)",
    )
    args = parser.parse_args()

    print("=== ASR-Agent deploy verification ===\n")
    check_python()
    print()
    check_imports()
    print()
    check_torch_cuda()
    print()
    check_ffmpeg()
    print()
    check_whisper_cache()
    print()
    check_ollama()
    print()
    check_optional_paths()
    print()
    check_asr_load(args.full)

    print()
    if WARNINGS:
        print(f"Warnings: {len(WARNINGS)}")
    if FAILURES:
        print(f"\nFAILED — {len(FAILURES)} issue(s) must be fixed before deploy:")
        for item in FAILURES:
            print(f"  - {item}")
        return 1

    print("\nAll critical checks passed. Ready to run: make run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
