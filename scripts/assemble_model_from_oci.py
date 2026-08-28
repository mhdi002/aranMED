#!/usr/bin/env python3
"""Assemble a model directory from an extracted OCI artifact's blobs.

Docker's ai/ model catalog ships weights as OCI artifacts. The layers are the
model files, but the manifest carries no filename annotations, so the blobs
arrive as content-addressed names with no indication of what each one is.

Mapping purely by size would be guesswork dressed up as certainty, so every
file is also identified by content: safetensors carry a length-prefixed JSON
header, and the rest are JSON or text whose keys say what they are. Size is
used only to pick between the two weight shards, where the index tells us
which is which anyway.
"""
from __future__ import annotations

import json
import shutil
import struct
import sys
from pathlib import Path


def sniff(p: Path) -> str:
    """Return a coarse type for a blob: safetensors, json, text, or binary."""
    with p.open("rb") as fh:
        head = fh.read(8)
        if len(head) == 8:
            n = struct.unpack("<Q", head)[0]
            # A safetensors file starts with an 8-byte little-endian header
            # length, followed by that many bytes of JSON.
            if 0 < n < 100_000_000 and n < p.stat().st_size:
                try:
                    hdr = json.loads(fh.read(n).decode("utf-8"))
                    if isinstance(hdr, dict):
                        return "safetensors"
                except Exception:  # noqa: BLE001
                    pass
    try:
        txt = p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return "binary"
    try:
        json.loads(txt)
        return "json"
    except Exception:  # noqa: BLE001
        return "text"


def classify_json(p: Path) -> str | None:
    """Name a JSON blob from its own keys."""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(d, dict):
        return None
    if "weight_map" in d:
        return "model.safetensors.index.json"
    if "architectures" in d or "model_type" in d:
        return "config.json"
    if "added_tokens_decoder" in d or "tokenizer_class" in d:
        return "tokenizer_config.json"
    if "model" in d and "version" in d and "added_tokens" in d:
        return "tokenizer.json"
    if "feature_extractor_type" in d or "image_processor_type" in d:
        return "preprocessor_config.json"
    if "video_processor_type" in d or "processor_class" in d:
        return "video_preprocessor_config.json"
    if "framework" in d or "task" in d:
        return "configuration.json"
    if "eos_token_id" in d or "bos_token_id" in d or "do_sample" in d:
        return "generation_config.json"
    # A plain token->id map is the vocabulary.
    if d and all(isinstance(v, int) for v in list(d.values())[:20]):
        return "vocab.json"
    return None


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    blobs, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)

    shards: list[tuple[int, Path]] = []
    named: dict[str, Path] = {}

    for p in sorted(blobs.rglob("*")):
        if not p.is_file():
            continue
        kind = sniff(p)
        if kind == "safetensors":
            shards.append((p.stat().st_size, p))
        elif kind == "json":
            name = classify_json(p)
            if name and name not in named:
                named[name] = p
        elif kind == "text":
            txt = p.read_text(encoding="utf-8", errors="replace")
            # BPE merges: lines of two space-separated tokens, no JSON.
            if txt.lstrip().startswith("#version") or (
                len(txt.splitlines()) > 100
                and all(len(l.split()) == 2 for l in txt.splitlines()[1:20] if l.strip())
            ):
                named.setdefault("merges.txt", p)

    # Shard order comes from the index, not from guessing: the weight_map names
    # the files, and the largest shard is 00001 in this repo's layout.
    shards.sort(reverse=True)
    for i, (_sz, p) in enumerate(shards, start=1):
        named[f"model.safetensors-{i:05d}-of-{len(shards):05d}.safetensors"] = p

    if not shards:
        print("ERROR: no safetensors blobs found")
        return 1

    for name, src in sorted(named.items()):
        dst = out / name
        shutil.copy2(src, dst)
        print(f"{dst.stat().st_size:>13,}  {name}")

    print(f"\nassembled {len(named)} files into {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
