"""Keyword-grounded retrieval evaluation (EN + FA gold sets)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from medrag.config import EVAL_DIR, RETRIEVAL
from medrag.rag.engine import RagEngine

SNIPPET_CHARS = 320


def load_gold(path: Path) -> list[dict]:
    return [json.loads(l) for l in open(path, encoding="utf-8")]


def _passage_record(c: dict, rank: int) -> dict:
    text = c.get("text") or ""
    return {
        "rank": rank,
        "title": c.get("title") or c.get("book") or "?",
        "page": c.get("page", c.get("page_start")),
        "score": round(float(c.get("score") or 0), 4),
        "rerank_raw": round(float(c["rerank_raw"]), 4) if "rerank_raw" in c else None,
        "source_corpus": c.get("source_corpus"),
        "specialty": c.get("specialty"),
        "doc_type": c.get("doc_type"),
        "snippet": text[:SNIPPET_CHARS].replace("\n", " ").strip(),
    }


def run_eval(gold_path: Path, label: str, eng: RagEngine | None = None) -> dict:
    gold = load_gold(gold_path)
    k = RETRIEVAL["top_k_final"]
    eng = eng or RagEngine()
    hits = 0
    details = []
    all_scores: list[float] = []
    for g in gold:
        try:
            ctx = eng.retrieve(g["question"], final_k=k)
            blob = "\n".join(c["text"] for c in ctx).lower()
            matched = [t for t in g["must_any"] if t.lower() in blob]
            ok = bool(matched)
            hits += ok
            retrieved = [_passage_record(c, i + 1) for i, c in enumerate(ctx)]
            for r in retrieved:
                all_scores.append(float(r.get("score") or 0))
            top = retrieved[0]["title"][:32] if retrieved else "-"
            details.append({
                "question": g["question"],
                "must_any": g.get("must_any", []),
                "matched_keywords": matched,
                "hit": ok,
                "top_source": top,
                "n_retrieved": len(retrieved),
                "retrieved": retrieved,
            })
            mark = "HIT" if ok else "MISS"
            print(f"[{mark}] {g['question'][:52]:52s} top={top}", flush=True)
        except Exception as e:
            details.append({
                "question": g["question"],
                "must_any": g.get("must_any", []),
                "matched_keywords": [],
                "hit": False,
                "top_source": "-",
                "n_retrieved": 0,
                "retrieved": [],
                "error": repr(e),
            })
            print(f"[ERR ] {g['question'][:52]:52s} {e}", flush=True)
    rate = hits / len(gold) if gold else 0
    print(f"\n{label} RETRIEVAL hit@{k}: {hits}/{len(gold)} = {rate:.1%}", flush=True)
    return {
        "label": label,
        "hit_at_k": rate,
        "hits": hits,
        "total": len(gold),
        "k": k,
        "details": details,
        "score_stats": {
            "n": len(all_scores),
            "mean": round(sum(all_scores) / len(all_scores), 4) if all_scores else 0,
            "min": round(min(all_scores), 4) if all_scores else 0,
            "max": round(max(all_scores), 4) if all_scores else 0,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=None)
    ap.add_argument("--lang", choices=["en", "fa", "all"], default="all")
    args = ap.parse_args()
    results = []
    if args.gold:
        results.append(run_eval(Path(args.gold), "custom"))
    else:
        if args.lang in ("en", "all"):
            p = EVAL_DIR / "gold_questions.jsonl"
            if p.exists():
                results.append(run_eval(p, "EN"))
        if args.lang in ("fa", "all"):
            p = EVAL_DIR / "gold_questions_fa.jsonl"
            if p.exists():
                results.append(run_eval(p, "FA"))
    return results


if __name__ == "__main__":
    main()
