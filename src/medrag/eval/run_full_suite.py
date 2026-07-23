"""Run full evaluation suite across all tracks."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime

from medrag.config import EVAL_DIR, REPORTS_DIR
from medrag.index import vectorstore as vs
from medrag.eval import retrieval_eval, verify, ocr_eval


def preflight():
    try:
        n = vs.count()
        if n == 0:
            print("WARNING: vector store is empty — retrieval eval will fail")
        return n
    except Exception as e:
        raise SystemExit(f"Qdrant not reachable: {e}. Run: docker compose up -d") from e


def run_all(n_auto=20, skip_ocr=False):
    chunk_count = preflight()
    report = {
        "timestamp": datetime.now().isoformat(),
        "chunk_count": chunk_count,
        "tracks": {},
    }

    print("\n=== Track A: Retrieval (EN) ===")
    try:
        en_results = retrieval_eval.run_eval(EVAL_DIR / "gold_questions.jsonl", "EN")
        report["tracks"]["retrieval_en"] = en_results
    except Exception as e:
        report["tracks"]["retrieval_en"] = {"error": str(e)}

    print("\n=== Track A: Retrieval (FA) ===")
    try:
        fa_results = retrieval_eval.run_eval(EVAL_DIR / "gold_questions_fa.jsonl", "FA")
        report["tracks"]["retrieval_fa"] = fa_results
    except Exception as e:
        report["tracks"]["retrieval_fa"] = {"error": str(e)}

    print("\n=== Track B: RAG verify ===")
    try:
        from medrag.rag.engine import RagEngine
        eng = RagEngine()
        auto_summary, auto_details = verify.eval_auto(n_auto, eng)
        curated = verify.eval_curated(eng)
        report["tracks"]["verify"] = {"auto": auto_summary, "curated": curated}
    except Exception as e:
        report["tracks"]["verify"] = {"error": str(e)}

    if not skip_ocr:
        print("\n=== Track C: OCR eval ===")
        try:
            report["tracks"]["ocr"] = ocr_eval.run_ocr_eval()
        except Exception as e:
            report["tracks"]["ocr"] = {"error": str(e)}

    ts = time.strftime("%Y%m%d_%H%M%S")
    json_path = REPORTS_DIR / f"eval_{ts}.json"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    md_lines = [
        f"# MedicalRAG Evaluation Report",
        f"**Date:** {report['timestamp']}",
        f"**Chunks indexed:** {chunk_count}",
        "",
    ]
    for track, data in report["tracks"].items():
        md_lines.append(f"## {track}")
        if "error" in data:
            md_lines.append(f"Error: {data['error']}")
        elif "hit_at_k" in data:
            md_lines.append(f"- hit@8: {data['hit_at_k']:.1%} ({data['hits']}/{data['total']})")
        elif "auto" in data:
            md_lines.append(f"- hit_rate@8: {data['auto'].get('hit_rate@8', 'N/A')}")
            md_lines.append(f"- avg_correct: {data['auto'].get('avg_correct', 'N/A')}")
        elif "avg_term_recall" in data:
            md_lines.append(f"- OCR term recall: {data['avg_term_recall']:.1%}")
            md_lines.append(f"- Passed: {data.get('passed', 0)}/{data.get('samples', 0)}")
        md_lines.append("")

    md_path = REPORTS_DIR / f"eval_{ts}.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"\nReports written:\n  {json_path}\n  {md_path}")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--skip-ocr", action="store_true")
    args = ap.parse_args()
    run_all(n_auto=args.n, skip_ocr=args.skip_ocr)


if __name__ == "__main__":
    main()
