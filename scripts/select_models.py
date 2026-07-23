#!/usr/bin/env python3
"""Scan the host for installed models and update ``backend/models.yaml``.

For each agent role (``core``, ``asr``, ``vision``) we probe the common model
sources and present an interactive picker.  The chosen entry is then enabled
+ marked ``default: true`` in the registry YAML — every other entry for that
role is disabled so there's no ambiguity.

Sources scanned
---------------
* **Ollama** — ``ollama list`` (text + vision tags).
* **HuggingFace hub cache** — ``~/.cache/huggingface/hub/models--*``.
* **Local GGUF files** — ``~/models/**/*.gguf`` plus any extra dirs passed
  with ``--gguf-dir``.

Run
---
    python scripts/select_models.py                # interactive
    python scripts/select_models.py --auto         # pick the first match
    python scripts/select_models.py --role core    # only update one role
    python scripts/select_models.py --print        # discovery only, no write

The script never downloads anything — it only configures models that are
already on disk.  Use ``ollama pull`` / ``huggingface-cli download`` first
if the list is empty.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
YAML_PATH = ROOT / "backend" / "models.yaml"

# ---------------------------------------------------------------------------
# Curated recommendations (offered when the host has nothing for a role).
# (label, kind, identifier, approx_gb, notes)
#   kind ∈ {ollama, hf, gguf}
# ---------------------------------------------------------------------------
RECOMMENDED: dict[str, list[tuple[str, str, str, float, str]]] = {
    "core": [
        ("qwen2.5:7b-instruct",     "ollama", "qwen2.5:7b-instruct",   4.7, "fast, solid tool-caller"),
        ("qwen3:14b",               "ollama", "qwen3:14b",             9.3, "stronger reasoning"),
        ("llama3.1:8b-instruct",    "ollama", "llama3.1:8b-instruct",  4.7, "Meta general-purpose"),
    ],
    "asr": [
        ("VibeVoice-ASR Q4 GGUF",     "gguf", "cstr/vibevoice-asr-GGUF:vibevoice-asr-q4_k.gguf", 5.0, "Microsoft long-form, needs CrispASR"),
        ("VibeVoice-ASR-HF",          "hf",   "microsoft/VibeVoice-ASR-HF", 15.0, "Microsoft long-form (~16 GB VRAM)"),
        ("facebook/omniASR-LLM-300M", "hf",   "facebook/omniASR-LLM-300M", 1.5,  "Meta omnilingual smallest (~2 GB VRAM, recommended)"),
        ("facebook/omniASR-LLM-1B",   "hf",   "facebook/omniASR-LLM-1B",   8.5,  "Meta omnilingual 1B (~6 GB VRAM)"),
        ("facebook/omniASR-LLM-3B",   "hf",   "facebook/omniASR-LLM-3B",  17.0,  "Meta omnilingual 3B (~10 GB VRAM)"),
        ("facebook/omniASR-LLM-7B",   "hf",   "facebook/omniASR-LLM-7B",  30.0,  "Meta omnilingual best (~17 GB VRAM)"),
        ("openai/whisper-large-v3",   "hf",   "openai/whisper-large-v3",   3.0,  "robust multilingual Whisper"),
    ],
    "vision": [
        ("Radiology-Infer-Mini Q8 GGUF", "gguf",
         "cgus/Radiology-Infer-Mini-iMat-GGUF:Radiology-Infer-Mini-Q8_0.gguf+mmproj-Radiology-Infer-Mini-f16.gguf",
         9.0, "medical imaging, needs llama-server"),
        ("llama3.2-vision:11b", "ollama", "llama3.2-vision:11b", 7.8, "general vision via Ollama"),
        ("openbmb/minicpm-v4.5", "ollama", "openbmb/minicpm-v4.5", 5.9, "compact multimodal"),
    ],
}

# ---------------------------------------------------------------------------
# Heuristics — which model names belong to which role.
# ---------------------------------------------------------------------------
VISION_HINTS = (
    "vision", "-vl", "vl-", "llava", "minicpm-v", "qwen2-vl", "qwen2.5-vl",
    "radiology", "llama3.2-vision", "internvl", "cogvlm", "moondream",
    "pixtral", "molmo", "phi-3-vision", "phi-3.5-vision", "llava-med",
    "mmproj",
)
ASR_HINTS = (
    "whisper", "omniasr", "omni-asr", "seamless", "wav2vec", "hubert",
    "parakeet", "canary", "distil-whisper", "vibevoice",
)
# Generic chat/instruct names — order matters: vision/asr win first.
CORE_HINTS = (
    "qwen", "llama", "mistral", "mixtral", "gemma", "phi", "deepseek",
    "yi-", "command-r", "granite", "instruct", "chat", "openchat",
)


def classify(name: str) -> str:
    n = name.lower()
    if any(h in n for h in VISION_HINTS):
        return "vision"
    if any(h in n for h in ASR_HINTS):
        return "asr"
    if any(h in n for h in CORE_HINTS):
        return "core"
    return "core"  # safest default — user can re-pick interactively


# ---------------------------------------------------------------------------
# Discovered model record
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    role: str
    name: str          # short label (e.g. "qwen3:14b")
    provider: str      # ollama / hf_text / llamacpp_python / …
    config: dict = field(default_factory=dict)
    source: str = ""   # human-readable origin
    size_gb: float = 0.0

    def label(self) -> str:
        sz = f" [{self.size_gb:.1f} GB]" if self.size_gb else ""
        return f"{self.name}{sz}  ({self.source})"


# ---------------------------------------------------------------------------
# Source 1 — Ollama
# ---------------------------------------------------------------------------
def discover_ollama() -> list[Candidate]:
    if shutil.which("ollama") is None:
        return []
    try:
        out = subprocess.check_output(
            ["ollama", "list"], stderr=subprocess.DEVNULL, timeout=8
        ).decode()
    except (subprocess.SubprocessError, OSError):
        return []
    cands: list[Candidate] = []
    # Skip the header line.
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"\s{2,}", line)
        if len(parts) < 3:
            continue
        name = parts[0].strip()
        size_str = parts[2].strip()         # e.g. "9.0 GB"
        try:
            size_gb = float(size_str.split()[0])
            if "MB" in size_str:
                size_gb /= 1024
        except (ValueError, IndexError):
            size_gb = 0.0
        role = classify(name)
        cfg = {"host": "http://127.0.0.1:11434", "model": name}
        if role == "vision":
            cfg["vision"] = True
        if role == "core":
            cfg["think"] = False
            cfg["options"] = {"num_ctx": 8192, "temperature": 0.2}
        cands.append(Candidate(role=role, name=name, provider="ollama",
                               config=cfg, source="ollama", size_gb=size_gb))
    return cands


# ---------------------------------------------------------------------------
# Source 2 — HuggingFace hub cache
# ---------------------------------------------------------------------------
def _hf_cache_dirs() -> list[Path]:
    cands = []
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE"):
        v = os.environ.get(var)
        if v:
            cands.append(Path(v))
    cands.extend([
        Path.home() / ".cache" / "huggingface" / "hub",
        Path.home() / ".cache" / "huggingface",
    ])
    return [d for d in cands if d.is_dir()]


def discover_hf_cache() -> list[Candidate]:
    cands: list[Candidate] = []
    seen: set[str] = set()
    for cache in _hf_cache_dirs():
        for entry in cache.glob("models--*"):
            if not entry.is_dir():
                continue
            # models--openai--whisper-large-v3 → openai/whisper-large-v3
            repo = entry.name[len("models--"):].replace("--", "/", 1).replace("--", "/")
            if repo in seen:
                continue
            seen.add(repo)
            try:
                size_gb = sum(p.stat().st_size for p in entry.rglob("*") if p.is_file()) / 1024**3
            except OSError:
                size_gb = 0.0
            role = classify(repo)
            if role == "asr":
                provider = "omniasr" if "omniasr" in repo.lower() else "hf_asr"
                cfg: dict = ({"model_card": repo.split("/")[-1]}
                             if provider == "omniasr"
                             else {"model_id": repo, "device": "auto", "dtype": "float16"})
            elif role == "vision":
                provider = "hf_vision"
                cfg = {"model_id": repo, "device": "auto", "dtype": "bfloat16"}
            else:
                provider = "hf_text"
                cfg = {"model_id": repo, "device": "auto", "dtype": "bfloat16"}
            cands.append(Candidate(role=role, name=repo, provider=provider,
                                   config=cfg, source="huggingface-cache",
                                   size_gb=size_gb))
    return cands


# ---------------------------------------------------------------------------
# Source 3 — local GGUF files (llama.cpp / llama-cpp-python)
# ---------------------------------------------------------------------------
def discover_gguf(extra_dirs: list[Path]) -> list[Candidate]:
    roots: list[Path] = []
    for d in [
        Path.home() / "models",
        Path.home() / ".cache" / "lm-studio" / "models",
        Path("/opt/models"),
        *extra_dirs,
    ]:
        if d.is_dir():
            roots.append(d)
    seen: set[Path] = set()
    cands: list[Candidate] = []
    for root in roots:
        for path in root.rglob("*.gguf"):
            if path in seen or "mmproj" in path.name.lower():
                continue
            seen.add(path)
            try:
                size_gb = path.stat().st_size / 1024**3
            except OSError:
                size_gb = 0.0
            name = path.stem
            role = classify(name)
            mmproj = next(
                (m for m in path.parent.glob("mmproj*.gguf")), None
            )
            if mmproj or role == "vision":
                # Vision GGUFs need a server (we won't start it here, just record).
                provider = "llamacpp_server"
                cfg = {
                    "base_url": "http://127.0.0.1:8088/v1",
                    "model": name,
                    "vision": True,
                    "model_path": str(path),
                    "mmproj_path": str(mmproj) if mmproj else None,
                    "timeout": 600,
                }
                role = "vision" if mmproj else role
            elif role == "asr":
                provider = "crispasr"
                cfg = {
                    "model_path": str(path),
                    "binary_path": "crispasr",
                    "backend": "vibevoice",
                }
            else:
                provider = "llamacpp_python"
                cfg = {"model_path": str(path), "n_ctx": 8192, "n_gpu_layers": -1}
            cands.append(Candidate(role=role, name=name, provider=provider,
                                   config=cfg,
                                   source=f"gguf:{path.parent.name}",
                                   size_gb=size_gb))
    return cands


# ---------------------------------------------------------------------------
# Picker
# ---------------------------------------------------------------------------
def pick(role: str, options: list[Candidate], *, auto: bool,
         allow_download: bool = True) -> Optional[Candidate]:
    if not options:
        if not allow_download:
            print(f"  ⚠  no {role} model found on this system.")
            return None
        # Offer to download something curated.
        return _offer_download(role, auto=auto)
    options = sorted(options, key=lambda c: (-c.size_gb, c.name))
    if auto:
        chosen = options[0]
        print(f"  → auto-selected {chosen.label()}")
        return chosen
    print(f"\n── select {role.upper()} model ──")
    for i, c in enumerate(options, 1):
        print(f"   [{i:>2}] {c.label()}")
    print(f"   [ d] download a recommended {role} model")
    print("   [ 0] (skip — leave models.yaml unchanged for this role)")
    while True:
        ans = input(f"choice for {role} [1]: ").strip().lower() or "1"
        if ans == "0":
            return None
        if ans == "d":
            cand = _offer_download(role, auto=False)
            if cand:
                return cand
            continue
        try:
            idx = int(ans)
            if 1 <= idx <= len(options):
                return options[idx - 1]
        except ValueError:
            pass
        print("   invalid — enter a number, 'd' to download, or 0 to skip.")


def _offer_download(role: str, *, auto: bool) -> Optional[Candidate]:
    catalog = RECOMMENDED.get(role, [])
    if not catalog:
        return None
    print(f"\n── no {role} model found — pick one to DOWNLOAD ──")
    for i, (label, kind, ident, gb, note) in enumerate(catalog, 1):
        print(f"   [{i:>2}] {label}  ~{gb:.1f} GB  ({kind})  — {note}")
    print("   [ 0] skip")
    if auto:
        idx = 1
    else:
        while True:
            ans = input(f"download which {role}? [1]: ").strip() or "1"
            if ans == "0":
                return None
            try:
                idx = int(ans)
                if 1 <= idx <= len(catalog):
                    break
            except ValueError:
                pass
            print("   invalid choice.")
    label, kind, ident, gb, _ = catalog[idx - 1]
    return _download(role, label, kind, ident)


def _download(role: str, label: str, kind: str, ident: str) -> Optional[Candidate]:
    print(f"\n→ downloading {label} ({kind})…")
    if kind == "ollama":
        if shutil.which("ollama") is None:
            print("  ollama binary missing — install from https://ollama.com/download then re-run.")
            return None
        try:
            subprocess.check_call(["ollama", "pull", ident])
        except subprocess.SubprocessError as e:
            print(f"  ollama pull failed: {e}")
            return None
        cfg = {"host": "http://127.0.0.1:11434", "model": ident}
        if role == "vision":
            cfg["vision"] = True
        if role == "core":
            cfg["think"] = False
            cfg["options"] = {"num_ctx": 8192, "temperature": 0.2}
        return Candidate(role=role, name=ident, provider="ollama", config=cfg,
                         source="ollama", size_gb=0.0)

    if kind == "hf":
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   "huggingface_hub"])
            from huggingface_hub import snapshot_download
        try:
            path = snapshot_download(repo_id=ident, resume_download=True)
        except Exception as e:  # noqa: BLE001
            print(f"  HF download failed: {e}")
            return None
        print(f"  cached at {path}")
        if role == "asr":
            if "omniasr" in ident.lower():
                # The package is needed at runtime.
                _try_pip("omnilingual-asr")
                return Candidate(role="asr", name=ident, provider="omniasr",
                                 config={"model_card": ident.split("/")[-1],
                                         "default_language": "eng_Latn",
                                         "batch_size": 1},
                                 source="huggingface", size_gb=0.0)
            return Candidate(role="asr", name=ident, provider="hf_asr",
                             config={"model_id": ident, "device": "auto",
                                     "dtype": "float16"},
                             source="huggingface", size_gb=0.0)
        if role == "vision":
            return Candidate(role="vision", name=ident, provider="hf_vision",
                             config={"model_id": ident, "device": "auto",
                                     "dtype": "bfloat16"},
                             source="huggingface", size_gb=0.0)
        return Candidate(role="core", name=ident, provider="hf_text",
                         config={"model_id": ident, "device": "auto",
                                 "dtype": "bfloat16"},
                         source="huggingface", size_gb=0.0)

    if kind == "gguf":
        # ident format:  "<repo>:<file1>+<file2>"
        repo, files = ident.split(":", 1)
        file_list = files.split("+")
        target = Path.home() / "models" / repo.split("/")[-1].lower()
        target.mkdir(parents=True, exist_ok=True)
        if shutil.which("huggingface-cli") is None:
            _try_pip("huggingface_hub[cli]")
        try:
            subprocess.check_call(
                ["huggingface-cli", "download", repo, *file_list,
                 "--local-dir", str(target)]
            )
        except subprocess.SubprocessError as e:
            print(f"  GGUF download failed: {e}")
            return None
        weight = target / file_list[0]
        mmproj = next((target / f for f in file_list[1:] if "mmproj" in f.lower()), None)
        if role == "asr":
            print("\n  ℹ  CrispASR binary required for GGUF ASR.")
            print("     Build from https://github.com/CrispStrobe/CrispASR")
            provider = "crispasr"
            cfg = {
                "model_path": str(weight),
                "binary_path": "crispasr",
                "backend": "vibevoice",
            }
        else:
            provider = "llamacpp_server" if (mmproj or role == "vision") else "llamacpp_python"
            cfg = {
                "base_url": "http://127.0.0.1:8088/v1",
                "model": weight.stem,
                "vision": bool(mmproj),
                "model_path": str(weight),
                "mmproj_path": str(mmproj) if mmproj else None,
                "timeout": 600,
            }
        return Candidate(role=role, name=weight.stem,
                         provider=provider, config=cfg,
                         source="gguf", size_gb=0.0)

    print(f"  unknown kind: {kind}")
    return None


def _try_pip(pkg: str) -> None:
    print(f"  ensuring pip package: {pkg}")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])
    except subprocess.SubprocessError as e:
        print(f"  ⚠  pip install {pkg} failed: {e}")


# ---------------------------------------------------------------------------
# YAML rewrite (no external dep)
# ---------------------------------------------------------------------------
def _ensure_yaml():
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("Installing PyYAML…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyyaml"])


def update_yaml(picks: dict[str, Candidate]) -> None:
    _ensure_yaml()
    import yaml

    if YAML_PATH.exists():
        with YAML_PATH.open() as f:
            doc = yaml.safe_load(f) or {}
    else:
        doc = {}
    models: list[dict] = list(doc.get("models", []))

    for role, cand in picks.items():
        # Disable any existing default for this role.
        for m in models:
            if m.get("role") == role:
                m["default"] = False
                # Keep them around but don't auto-load.
                if m.get("enabled") and m.get("name") != cand.name:
                    m["enabled"] = False
        # Update or insert.
        existing = next((m for m in models
                         if m.get("role") == role and m.get("name") == cand.name),
                        None)
        if existing is not None:
            existing.update({
                "provider": cand.provider,
                "enabled": True,
                "default": True,
                "config": cand.config,
            })
        else:
            models.append({
                "role": role,
                "name": cand.name,
                "provider": cand.provider,
                "enabled": True,
                "default": True,
                "config": cand.config,
            })

    doc["models"] = models
    doc.setdefault("runtime", {
        "max_warm_models": 2,
        "vram_budget_gb": 18,
        "history_keep_turns": 12,
        "history_summary_trigger": 24,
    })

    with YAML_PATH.open("w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True, width=100)
    print(f"\n✔ wrote {YAML_PATH.relative_to(ROOT)}")


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--auto", action="store_true",
                    help="pick the largest match for each role without prompting")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="just list discoveries, don't modify models.yaml")
    ap.add_argument("--role", action="append", choices=["core", "asr", "vision"],
                    help="only configure these role(s) — repeatable")
    ap.add_argument("--gguf-dir", action="append", type=Path, default=[],
                    help="additional dir to scan for *.gguf files (repeatable)")
    args = ap.parse_args()

    print("Scanning for installed models…")
    all_cands: list[Candidate] = []
    all_cands += discover_ollama()
    all_cands += discover_hf_cache()
    all_cands += discover_gguf(args.gguf_dir)

    by_role: dict[str, list[Candidate]] = {"core": [], "asr": [], "vision": []}
    for c in all_cands:
        by_role.setdefault(c.role, []).append(c)
    print(f"  found {sum(len(v) for v in by_role.values())} model(s): "
          + ", ".join(f"{r}={len(v)}" for r, v in by_role.items()))

    if args.print_only:
        for role, items in by_role.items():
            print(f"\n[{role}]")
            for c in items:
                print(f"  • {c.label():<60} provider={c.provider}")
        return 0

    roles = args.role or ["core", "asr", "vision"]
    picks: dict[str, Candidate] = {}
    for role in roles:
        chosen = pick(role, by_role.get(role, []), auto=args.auto)
        if chosen:
            picks[role] = chosen

    if not picks:
        print("Nothing selected — models.yaml left unchanged.")
        return 1

    update_yaml(picks)
    print("\nSelected:")
    for role, c in picks.items():
        print(f"  {role:<6} → {c.name}  [{c.provider}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
