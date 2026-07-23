"""Download models required for production (Whisper + Ollama LLM)."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS_YAML = ROOT / "backend" / "models.yaml"
WHISPER_ID = "openai/whisper-large-v3"


def _ollama_model_from_yaml() -> str:
    import yaml

    if not MODELS_YAML.is_file():
        return os.getenv("OLLAMA_MODEL", "qwen3.5-9b-custom")
    data = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8")) or {}
    for entry in data.get("models", []):
        if entry.get("role") == "core" and entry.get("provider") == "ollama":
            cfg = entry.get("config") or {}
            return cfg.get("model") or entry.get("name") or "qwen3.5-9b-custom"
    return os.getenv("OLLAMA_MODEL", "qwen3.5-9b-custom")


def download_whisper() -> None:
    print(f"=== Downloading {WHISPER_ID} to Hugging Face cache ===")
    from huggingface_hub import snapshot_download

    path = snapshot_download(
        WHISPER_ID,
    )
    weights = Path(path) / "model.safetensors"
    if weights.is_file() and weights.stat().st_size > 1_000_000_000:
        print(f"OK: {weights} ({weights.stat().st_size:,} bytes)")
    else:
        print(f"WARN: weights missing or too small at {weights}", file=sys.stderr)


def pull_ollama(model: str) -> None:
    print(f"=== Pulling Ollama model: {model} ===")
    host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    try:
        import httpx

        httpx.get(f"{host}/api/tags", timeout=3.0).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: Ollama not running at {host} ({exc})", file=sys.stderr)
        print("  Start Ollama, then run: ollama pull", model)
        return

    proc = subprocess.run(["ollama", "pull", model], check=False)
    if proc.returncode != 0:
        print(f"WARN: ollama pull {model} failed (exit {proc.returncode})", file=sys.stderr)
        print("  If you use a custom Modelfile, import it manually.", file=sys.stderr)
    else:
        print(f"OK: {model}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install ASR-Agent models")
    parser.add_argument("--whisper-only", action="store_true")
    parser.add_argument("--skip-ollama", action="store_true")
    parser.add_argument("--ollama-model", default="")
    args = parser.parse_args()

    download_whisper()
    if args.whisper_only or args.skip_ollama:
        return 0

    model = args.ollama_model or _ollama_model_from_yaml()
    pull_ollama(model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
