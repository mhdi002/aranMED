#!/usr/bin/env python3
"""Pre-download model weights into the shared cache before serving traffic.

Why this exists
---------------
MedicalRAG downloads its embedding model on the *first* `/ask`. That makes the
first real request block on a multi-GB download and time out, and every retry
restarts the transfer because an aborted HTTP request cancels it. Warming the
cache is therefore a deployment step, not something to discover in production.

Nothing is hardcoded: model ids and the cache directory come from the same
environment the services use (`MEDRAG_EMBED_MODEL`, `MEDRAG_RERANK_MODEL`,
`HF_HOME`), so warming always matches what will actually be loaded.

Usage
-----
    python scripts/warmup_models.py                      # on the host
    docker compose exec medrag python /app/scripts/warmup_models.py
    python scripts/warmup_models.py --models BAAI/bge-m3 --workers 8
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def default_models() -> list[str]:
    """Models this deployment will actually load, from its own config.

    Resolution order per model: environment variable, then medrag's own
    config module (which merges config.yaml). Reading the module matters —
    the reranker is normally declared in config.yaml's `reranker.model`, not
    as an env var, so an env-only lookup would silently skip it and leave the
    first /ask blocking on a ~2.2 GB download after embeddings were warmed.
    """
    from_config: dict[str, str] = {}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from medrag import config as mcfg

        from_config = {
            "MEDRAG_EMBED_MODEL": getattr(mcfg, "EMBED_MODEL", "") or "",
            "MEDRAG_RERANK_MODEL": getattr(mcfg, "RERANK_MODEL", "") or "",
        }
    except Exception:  # noqa: BLE001
        pass

    out: list[str] = []
    for var in ("MEDRAG_EMBED_MODEL", "MEDRAG_RERANK_MODEL"):
        v = (os.getenv(var) or from_config.get(var, "")).strip()
        if v and v not in out:
            out.append(v)
    for m in backend_registry_models():
        if m not in out:
            out.append(m)
    return out


def backend_registry_models() -> list[str]:
    """HuggingFace ids declared by *enabled* entries in the backend registry.

    The backend downloads its ASR/vision weights on first request, into a
    container volume that is separate from any host cache — so a fresh
    `docker compose up` re-fetches multi-GB models mid-request. Warming them
    is the same problem as the medrag models and belongs in the same step.

    Only `model_id`-style HuggingFace references are returned; Ollama tags and
    local GGUF paths are not fetched from the Hub.
    """
    path = os.getenv("ASR_AGENT_REGISTRY_YAML") or ""
    candidates = [Path(path)] if path else []
    root = Path(__file__).resolve().parents[1]
    candidates += [root / "backend" / "models.yaml", root / "backend" / "models.docker.yaml"]

    for p in candidates:
        if not p.is_file():
            continue
        try:
            import yaml

            spec = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        out: list[str] = []
        for entry in spec.get("models", []) or []:
            if not entry.get("enabled", True):
                continue
            cfg = entry.get("config") or {}
            mid = str(cfg.get("model_id") or "").strip()
            # A Hub id looks like "org/name"; skip tags and filesystem paths.
            if mid and "/" in mid and not mid.startswith((".", "/", "~")) and mid not in out:
                out.append(os.path.expandvars(mid))
        if out:
            return out
    return []


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def dir_size(p: Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except Exception:  # noqa: BLE001
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="*", default=None,
                    help="Model ids (default: from MEDRAG_EMBED_MODEL / MEDRAG_RERANK_MODEL)")
    ap.add_argument("--cache", default=None,
                    help="Cache dir (default: $HF_HOME/hub, else ~/.cache/huggingface/hub)")
    ap.add_argument("--workers", type=int, default=int(os.getenv("WARMUP_WORKERS", "4")))
    ap.add_argument("--retries", type=int, default=int(os.getenv("WARMUP_RETRIES", "5")),
                    help="Retry count — resumable, so a flaky link eventually completes")
    ap.add_argument("--all-formats", action="store_true",
                    help="Also fetch ONNX/OpenVINO exports. Off by default: a repo like "
                         "BAAI/bge-m3 ships ~2 GB of alternate formats the PyTorch "
                         "runtime never loads, and on a slow link they crowd out the "
                         "weights that are actually needed.")
    args = ap.parse_args()

    # Only the artefacts the torch/FlagEmbedding path actually loads.
    allow = None if args.all_formats else [
        "*.json", "*.model", "*.txt", "*.safetensors", "*.bin",
        "1_Pooling/*", "colbert_linear.pt", "sparse_linear.pt",
    ]

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("huggingface_hub is not installed in this interpreter.", file=sys.stderr)
        print("Run inside the medrag container:", file=sys.stderr)
        print("  docker compose exec medrag python /app/scripts/warmup_models.py", file=sys.stderr)
        return 2

    models = args.models or default_models()
    if not models:
        print("No models resolved — set MEDRAG_EMBED_MODEL or pass --models.")
        return 1

    hf_home = args.cache or os.path.join(os.getenv("HF_HOME", str(Path.home() / ".cache" / "huggingface")), "hub")
    Path(hf_home).mkdir(parents=True, exist_ok=True)
    print(f"cache : {hf_home}")
    print(f"models: {', '.join(models)}\n")

    failed = []
    for model in models:
        print(f"→ {model}")
        for attempt in range(1, args.retries + 1):
            t0 = time.perf_counter()
            try:
                # snapshot_download resumes partial blobs, so retrying a slow or
                # interrupted link makes progress instead of starting over.
                path = snapshot_download(
                    model, cache_dir=hf_home, max_workers=args.workers,
                    allow_patterns=allow,
                )
                size = dir_size(Path(path))
                print(f"  ok  {human(size)} in {time.perf_counter() - t0:.0f}s -> {path}\n")
                break
            except Exception as e:  # noqa: BLE001
                print(f"  attempt {attempt}/{args.retries} failed: {type(e).__name__}: {e}")
                if attempt == args.retries:
                    failed.append(model)
                    print()

    if failed:
        print(f"FAILED: {', '.join(failed)}")
        print("Downloads resume — re-run this script to continue.")
        return 1
    print("All models cached. First /ask will no longer block on a download.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
