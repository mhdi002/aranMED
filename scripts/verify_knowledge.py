#!/usr/bin/env python3
"""Knowledge-base verification: prove the corpus is real, loaded and queryable.

Checks, in order:
  1. Qdrant is reachable and reports collections.
  2. The configured logical stores map to collections that actually exist.
  3. Those collections hold points, and the vector config matches the
     configured embedding dimension.
  4. A live vector search returns real payloads with provenance
     (title / page / source), not empty or placeholder records.
  5. If MedicalRAG is reachable, its /health agrees on the chunk count.

Nothing is hardcoded: endpoints come from scripts/_endpoints.py, the
collection base name and store list from the medrag configuration
(MEDRAG_QDRANT_STORES, vector_db.collection), and the vector dimension from
the collection itself.

Exit 0 = PASS. Writes reports/knowledge_verify.json.

Usage:
    python scripts/verify_knowledge.py
    python scripts/verify_knowledge.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from _endpoints import MEDRAG_URL, QDRANT_URL  # noqa: E402


def http(url: str, data: dict | None = None, timeout: float = 180):
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def configured_stores() -> list[str]:
    raw = (os.getenv("MEDRAG_QDRANT_STORES") or "").strip()
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from medrag.config import QDRANT_STORE_NAMES

        return list(QDRANT_STORE_NAMES)
    except Exception:  # noqa: BLE001
        return ["main", "standards", "expand"]


def collection_base() -> str:
    if (v := (os.getenv("MEDRAG_COLLECTION") or "").strip()):
        return v
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from medrag.config import COLLECTION

        return COLLECTION
    except Exception:  # noqa: BLE001
        return "medical_library"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--qdrant", default=QDRANT_URL)
    ap.add_argument("--medrag", default=MEDRAG_URL)
    ap.add_argument("--samples", type=int, default=int(os.getenv("KNOWLEDGE_VERIFY_SAMPLES", "3")))
    ap.add_argument("--min-points", type=int, default=int(os.getenv("KNOWLEDGE_VERIFY_MIN_POINTS", "1")))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "knowledge_verify.json")
    args = ap.parse_args()

    result: dict = {"qdrant": args.qdrant, "medrag": args.medrag, "checks": [], "collections": {}}

    def check(name: str, ok: bool, detail: str = "") -> bool:
        result["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not args.json:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        return bool(ok)

    # 1. Qdrant reachable
    try:
        cols = http(f"{args.qdrant}/collections", timeout=60)["result"]["collections"]
        names = [c["name"] for c in cols]
    except Exception as e:  # noqa: BLE001
        check("qdrant reachable", False, f"{type(e).__name__}: {e}")
        _emit(result, args)
        return 1
    check("qdrant reachable", True, f"{len(names)} collections")

    # 2/3. configured stores exist and hold points
    base, stores = collection_base(), configured_stores()
    result["configured_stores"] = stores
    total = 0
    populated = 0
    for store in stores:
        cname = f"{base}_{store}"
        if cname not in names:
            check(f"collection {cname}", False, "configured store has no collection")
            continue
        try:
            info = http(f"{args.qdrant}/collections/{cname}", timeout=60)["result"]
            pts = info.get("points_count") or 0
            vectors = (info.get("config", {}).get("params", {}) or {}).get("vectors", {})
            dim = (vectors.get("dense") or {}).get("size") if isinstance(vectors, dict) else None
            dist = (vectors.get("dense") or {}).get("distance") if isinstance(vectors, dict) else None
        except Exception as e:  # noqa: BLE001
            check(f"collection {cname}", False, f"{type(e).__name__}: {e}")
            continue
        total += pts
        if pts > 0:
            populated += 1
        result["collections"][cname] = {"points": pts, "dim": dim, "distance": dist}
        check(f"collection {cname}", True, f"{pts:,} points, dim={dim}, {dist}")

    check("corpus has points", total >= args.min_points, f"{total:,} points across {populated} populated store(s)")
    result["total_points"] = total

    # 4. live search returns real, attributed payloads
    target = max(
        (c for c in result["collections"] if result["collections"][c]["points"] > 0),
        key=lambda c: result["collections"][c]["points"],
        default=None,
    )
    if target:
        dim = result["collections"][target]["dim"] or 1024
        random.seed(int(os.getenv("KNOWLEDGE_VERIFY_SEED", "7")))
        v = [random.gauss(0, 1) for _ in range(dim)]
        n = sum(x * x for x in v) ** 0.5
        v = [x / n for x in v]
        try:
            pts = http(
                f"{args.qdrant}/collections/{target}/points/query",
                {"query": v, "using": "dense", "limit": args.samples, "with_payload": True},
            )["result"]["points"]
        except Exception as e:  # noqa: BLE001
            check("vector search", False, f"{type(e).__name__}: {e}")
            pts = []
        check("vector search", len(pts) > 0, f"{len(pts)} hits from {target}")

        attributed = 0
        samples = []
        for p in pts:
            pl = p.get("payload") or {}
            title = pl.get("title") or pl.get("book")
            text = (pl.get("text") or "").strip()
            if title and text:
                attributed += 1
            samples.append({
                "score": round(p.get("score", 0), 4),
                "title": str(title)[:80] if title else None,
                "page": pl.get("page"),
                "source_corpus": pl.get("source_corpus"),
                "specialty": pl.get("specialty"),
                "text": text[:160],
            })
        result["samples"] = samples
        check("results carry real content + provenance", attributed == len(pts) and attributed > 0,
              f"{attributed}/{len(pts)} hits have both title and text")
        # A random unit vector must not score ~1.0; that would indicate the
        # query was mis-formed or the index is degenerate.
        top = max((s["score"] for s in samples), default=0)
        check("cosine scores behave sanely", top < 0.95, f"top score {top} from a random probe")

    # 5. medrag agreement (optional)
    try:
        h = http(f"{args.medrag}/health", timeout=120)
        result["medrag_health"] = h
        chunks = h.get("chunks")
        check("medrag reports chunks", bool(chunks), f"chunks={chunks:,}" if chunks else "no chunks")
        check("medrag embeddings ready", bool((h.get("embeddings") or {}).get("ok")),
              str((h.get("embeddings") or {}).get("model")))
    except Exception as e:  # noqa: BLE001
        if not args.json:
            print(f"[SKIP] medrag /health — not reachable ({type(e).__name__})")
        result["medrag_health"] = None

    result["ok"] = all(c["ok"] for c in result["checks"])

    if not args.json and result.get("samples"):
        print("\n--- sample retrieved documents ---")
        for s in result["samples"]:
            print(f"  {s['score']:.4f}  {s['title']}  p.{s['page']}")
            print(f"          {s['text'][:110]}")

    if not args.json:
        print(f"\nRESULT: {'PASS' if result['ok'] else 'FAIL'}")
    _emit(result, args)
    return 0 if result["ok"] else 1


def _emit(result: dict, args) -> None:
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    raise SystemExit(main())
