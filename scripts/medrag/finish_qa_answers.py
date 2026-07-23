"""Finish verify/smoke answers one question per subprocess (OOM-safe on 8GB).

Usage:
  python scripts/finish_qa_answers.py --resume reports/QA_RETRIEVAL_DETAIL_....partial.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
PLOTS = REPORTS / "plots"
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("MEDRAG_EMBED_PROVIDER", "local")

SNIPPET = 360
SMOKE_QUESTIONS = [
    "What are ECG findings in hyperkalemia?",
    "Metformin starting dose with eGFR 25?",
    "Warfarin interacts with amiodarone — what should I watch?",
    "طبق استاندارد آزمایش تعیین مقاومت داروئی مایکوباکتریوم چه اصولی مهم است؟",
]


def _brief(p: dict, rank: int) -> dict:
    text = (p.get("text") or "")
    return {
        "rank": rank,
        "title": p.get("title") or p.get("book") or "?",
        "page": p.get("page", p.get("page_start")),
        "score": round(float(p.get("score") or 0), 4),
        "source_corpus": p.get("source_corpus"),
        "specialty": p.get("specialty"),
        "snippet": text[:SNIPPET].replace("\n", " ").strip(),
    }


def answer_one(question: str) -> dict:
    """Run inside isolated process — retrieve once, generate, exit."""
    os.environ.setdefault("MEDRAG_LLM_PROVIDER", "ollama")
    os.environ.setdefault("MEDRAG_LLM_MODEL", "qwen3.5-9b:latest")
    from medrag.config import SELF_RAG_ENABLED
    import medrag.rag.engine as engine_mod
    import medrag.config as cfg_mod
    cfg_mod.SELF_RAG_ENABLED = False
    if hasattr(engine_mod, "SELF_RAG_ENABLED"):
        engine_mod.SELF_RAG_ENABLED = False

    from medrag.rag.engine import RagEngine
    from medrag.rag.grounding import verify_answer

    eng = RagEngine()
    item = {"question": question}
    try:
        ctx = eng.retrieve(question)
        item["retrieved"] = [_brief(p, i + 1) for i, p in enumerate(ctx)]
        try:
            import gc
            import torch
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        resp, gen_ctx = eng._generate(question, ctx)
        g = verify_answer(question, resp, gen_ctx)
        item.update({
            "answer": resp or "",
            "sources": [
                {
                    "n": i + 1,
                    "title": p.get("title", "?"),
                    "page": p.get("page", p.get("page_start")),
                    "score": round(float(p.get("score") or 0), 3),
                    "source_corpus": p.get("source_corpus"),
                }
                for i, p in enumerate(gen_ctx)
            ],
            "n_sources": len(gen_ctx),
            "grounded": g.get("grounded"),
            "has_citations": g.get("has_citations"),
            "grounding": g,
            "answer_mode": "subprocess_generate",
        })
    except Exception as e:
        item["error"] = repr(e)
        item["traceback"] = traceback.format_exc()[-1000:]
    return item


def _run_child(question: str, out_path: Path, timeout: int = 900) -> dict:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    code = (
        "import json,sys\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "from pathlib import Path\n"
        "import importlib.util\n"
        f"spec=importlib.util.spec_from_file_location('fin', {str(Path(__file__).resolve())!r})\n"
    )
    # Simpler: invoke this file with --one
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONUTF8": "1",
        "MEDRAG_EMBED_PROVIDER": "local",
        "MEDRAG_LLM_PROVIDER": os.environ.get("MEDRAG_LLM_PROVIDER", "ollama"),
        "MEDRAG_LLM_MODEL": os.environ.get("MEDRAG_LLM_MODEL", "qwen3.5-9b:latest"),
    }
    env.pop("MEDRAG_EMBED_BASE_URL", None)
    env.pop("VLLM_EMBED_BASE_URL", None)
    qfile = out_path.with_suffix(".q.txt")
    qfile.write_text(question, encoding="utf-8")
    cmd = [
        sys.executable, "-u", str(Path(__file__).resolve()),
        "--one", "--question-file", str(qfile), "--out", str(out_path),
    ]
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=env, timeout=timeout,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if out_path.exists():
            return json.loads(out_path.read_text(encoding="utf-8"))
        return {
            "question": question,
            "error": f"child_rc={p.returncode}",
            "stderr": (p.stderr or "")[-1500:],
            "stdout": (p.stdout or "")[-800:],
        }
    except subprocess.TimeoutExpired:
        return {"question": question, "error": "timeout"}
    finally:
        try:
            qfile.unlink()
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", type=Path, required=False)
    ap.add_argument("--one", action="store_true")
    ap.add_argument("--question-file", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    if args.one:
        q = args.question_file.read_text(encoding="utf-8").strip()
        item = answer_one(q)
        args.out.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"ONE_DONE sources={item.get('n_sources')} grounded={item.get('grounded')} err={item.get('error')}", flush=True)
        return 0 if "error" not in item else 1

    if not args.resume or not args.resume.exists():
        print("Need --resume path to partial/full report JSON", flush=True)
        return 2

    report = json.loads(args.resume.read_text(encoding="utf-8"))
    ts = time.strftime("%Y%m%d_%H%M%S")
    report["timestamp_local"] = ts
    report["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    report["finish_mode"] = "subprocess_per_question"

    from medrag.eval.verify import CURATED

    # --- verify ---
    existing = {
        (r.get("question") or ""): r
        for r in ((report.get("verify") or {}).get("results") or [])
        if r.get("answer") and "error" not in r
    }
    verify_results = []
    tmp_dir = REPORTS / f"_answer_tmp_{ts}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    print(f"=== VERIFY ({len(CURATED)}), {len(existing)} cached ===", flush=True)
    for i, q in enumerate(CURATED, 1):
        if q in existing:
            print(f"[{i}] CACHE {q[:60]}", flush=True)
            verify_results.append(existing[q])
            continue
        print(f"[{i}] RUN {q[:60]}", flush=True)
        out = tmp_dir / f"verify_{i:02d}.json"
        item = _run_child(q, out)
        verify_results.append(item)
        print(
            f"  -> sources={item.get('n_sources')} grounded={item.get('grounded')} "
            f"err={item.get('error')}",
            flush=True,
        )
        report["verify"] = {
            "ok": all("error" not in r and (r.get("n_sources") or 0) > 0 for r in verify_results),
            "n": len(CURATED),
            "n_with_sources": sum(1 for r in verify_results if (r.get("n_sources") or 0) > 0),
            "llm_answers": True,
            "results": verify_results,
        }
        partial = REPORTS / f"QA_RETRIEVAL_DETAIL_{ts}.partial.json"
        partial.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- smoke ---
    smoke_existing = {
        (r.get("question") or ""): r
        for r in ((report.get("smoke") or {}).get("results") or [])
        if r.get("answer") and "error" not in r
    }
    smoke_results = []
    print(f"\n=== SMOKE ({len(SMOKE_QUESTIONS)}) ===", flush=True)
    for i, q in enumerate(SMOKE_QUESTIONS, 1):
        if q in smoke_existing:
            print(f"[{i}] CACHE {q[:60]}", flush=True)
            smoke_results.append(smoke_existing[q])
            continue
        print(f"[{i}] RUN {q[:60]}", flush=True)
        out = tmp_dir / f"smoke_{i:02d}.json"
        item = _run_child(q, out)
        smoke_results.append(item)
        print(
            f"  -> sources={item.get('n_sources')} grounded={item.get('grounded')} "
            f"err={item.get('error')}",
            flush=True,
        )
        report["smoke"] = {
            "ok": all("error" not in r and (r.get("n_sources") or 0) > 0 for r in smoke_results),
            "n": len(SMOKE_QUESTIONS),
            "n_with_sources": sum(1 for r in smoke_results if (r.get("n_sources") or 0) > 0),
            "llm_answers": True,
            "results": smoke_results,
        }
        partial = REPORTS / f"QA_RETRIEVAL_DETAIL_{ts}.partial.json"
        partial.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    # Import plot/markdown writers from main harness
    sys.path.insert(0, str(ROOT / "scripts"))
    import run_qa_eval_with_plots as harness

    plot_paths = harness.make_plots(report, ts)
    report["plots"] = plot_paths
    json_path = REPORTS / f"QA_RETRIEVAL_DETAIL_{ts}.json"
    md_path = REPORTS / f"QA_RETRIEVAL_DETAIL_{ts}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    harness.write_markdown(report, md_path, plot_paths)
    print(f"\nSaved:\n  {json_path}\n  {md_path}", flush=True)
    for p in plot_paths:
        print(f"  plot: {p}", flush=True)
    return 0


if __name__ == "__main__":
    # Prefer the newer partial that already has some verify answers
    sys.exit(main())
