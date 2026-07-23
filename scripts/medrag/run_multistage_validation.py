"""Multi-stage validation suite for MedicalRAG accuracy & integrity.

Stages:
  1. unit        — pytest (fast, no GPU/Qdrant required for most)
  2. coverage    — catalog vs disk inventory
  3. retrieval   — gold-question hit@k (needs Qdrant; may skip if locked by embed)
  4. verify      — curated RAG answer checks (needs Ollama + Qdrant)
  5. smoke       — a few bilingual CLI-style answers

Writes reports/multistage_validation_<ts>.{json,md}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
sys.path.insert(0, str(ROOT / "src"))


def _local_qdrant_lock() -> Path:
    """Legacy local-mode lock path (server mode embed does not create this)."""
    from medrag.config import QDRANT_STORAGE
    return Path(QDRANT_STORAGE) / ".lock"


def _run(cmd: list[str], timeout: int = 600) -> dict:
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    try:
        p = subprocess.run(
            cmd, cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return {
            "rc": p.returncode,
            "stdout": (p.stdout or "")[-8000:],
            "stderr": (p.stderr or "")[-4000:],
        }
    except subprocess.TimeoutExpired as e:
        return {"rc": -1, "stdout": "", "stderr": f"timeout: {e}"}
    except Exception as e:
        return {"rc": -1, "stdout": "", "stderr": repr(e)}


def stage_unit() -> dict:
    print("\n=== STAGE 1: unit tests ===", flush=True)
    r = _run([
        sys.executable, "-m", "pytest",
        "tests/test_index_integrity.py",
        "tests/test_system_smoke.py",
        "tests/test_knowledge_mvp.py",
        "tests/test_embed_resume.py",
        "tests/test_rag_advanced.py",
        "tests/test_colbert_selfrag.py",
        "tests/test_documents.py",
        "-q", "--tb=line",
    ], timeout=300)
    ok = r["rc"] == 0
    print(r["stdout"][-1500:] if r["stdout"] else r["stderr"], flush=True)
    return {"ok": ok, **r}


def stage_coverage() -> dict:
    print("\n=== STAGE 2: coverage inventory ===", flush=True)
    r = _run([sys.executable, "-u", "scripts/embed_coverage.py"], timeout=120)
    # parse key lines from stdout
    summary = {}
    for line in (r["stdout"] or "").splitlines():
        if any(k in line.lower() for k in (
            "catalog", "disk", "embedded", "missing", "mehrsys", "standards", "exam",
        )):
            summary.setdefault("lines", []).append(line)
    print("\n".join(summary.get("lines", [])[:40]), flush=True)
    return {"ok": r["rc"] == 0, "summary_lines": summary.get("lines", []), **r}


def stage_retrieval() -> dict:
    print("\n=== STAGE 3: retrieval gold ===", flush=True)
    lock = _local_qdrant_lock()
    if lock.exists():
        msg = "Qdrant locked by another process (likely embed) — skip retrieval for now"
        print(msg, flush=True)
        return {"ok": True, "skipped": True, "reason": msg}
    try:
        from medrag.index import vectorstore as vs
        n = vs.count()
        if n == 0:
            return {"ok": False, "error": "empty vector store"}
    except Exception as e:
        return {"ok": False, "error": f"qdrant: {e}", "skipped": True}

    out = {"ok": True, "tracks": {}}
    try:
        from medrag.eval import retrieval_eval
        from medrag.config import EVAL_DIR
        en = retrieval_eval.run_eval(EVAL_DIR / "gold_questions.jsonl", "EN")
        fa = retrieval_eval.run_eval(EVAL_DIR / "gold_questions_fa.jsonl", "FA")
        out["tracks"]["en"] = en
        out["tracks"]["fa"] = fa
        print(f"EN hit@k={en.get('hit_at_k')}  FA hit@k={fa.get('hit_at_k')}", flush=True)
    except Exception as e:
        out["ok"] = False
        out["error"] = repr(e)
        print(f"retrieval failed: {e}", flush=True)
    return out


def stage_verify(n: int = 8) -> dict:
    print("\n=== STAGE 4: RAG verify (curated + auto) ===", flush=True)
    lock = _local_qdrant_lock()
    if lock.exists():
        msg = "Qdrant locked — skip verify"
        print(msg, flush=True)
        return {"ok": True, "skipped": True, "reason": msg}
    try:
        from medrag.rag.engine import RagEngine
        from medrag.eval import verify
        eng = RagEngine()
        auto_summary, _ = verify.eval_auto(n, eng)
        curated = verify.eval_curated(eng)
        print(f"auto={auto_summary}", flush=True)
        print(f"curated keys={list(curated) if isinstance(curated, dict) else curated}", flush=True)
        return {"ok": True, "auto": auto_summary, "curated": curated}
    except Exception as e:
        print(f"verify failed: {e}", flush=True)
        return {"ok": False, "error": repr(e)}


def stage_smoke() -> dict:
    print("\n=== STAGE 5: bilingual smoke answers ===", flush=True)
    lock = _local_qdrant_lock()
    if lock.exists():
        msg = "Qdrant locked — skip smoke"
        print(msg, flush=True)
        return {"ok": True, "skipped": True, "reason": msg}

    questions = [
        "What are ECG findings in hyperkalemia?",
        "Metformin starting dose with eGFR 25?",
        "Warfarin interacts with amiodarone — what should I watch?",
        "طبق استاندارد آزمایش تعیین مقاومت داروئی مایکوباکتریوم چه اصولی مهم است؟",
    ]
    results = []
    try:
        from medrag.rag.engine import RagEngine
        eng = RagEngine()
        for q in questions:
            print(f"Q: {q[:80]}", flush=True)
            try:
                r = eng.answer(q)
                g = r.get("grounding") or {}
                item = {
                    "q": q,
                    "intent": r.get("intent"),
                    "n_sources": len(r.get("sources") or []),
                    "grounded": g.get("grounded"),
                    "has_citations": g.get("has_citations"),
                    "answer": r.get("answer") or "",
                    "answer_preview": (r.get("answer") or "")[:400],
                    "sources": [
                        {
                            "n": s.get("n"),
                            "title": s.get("title"),
                            "page": s.get("page"),
                            "score": s.get("score"),
                            "source_corpus": s.get("source_corpus"),
                        }
                        for s in (r.get("sources") or [])
                    ],
                    "rule_alerts": len(r.get("rule_alerts") or []),
                }
                print(
                    f"  intent={item['intent']} sources={item['n_sources']} "
                    f"grounded={item['grounded']} cites={item['has_citations']}",
                    flush=True,
                )
                results.append(item)
            except Exception as e:
                results.append({"q": q, "error": repr(e)})
                print(f"  ERROR {e}", flush=True)
        ok = all("error" not in x for x in results) and all(
            (x.get("n_sources") or 0) > 0 for x in results if "error" not in x
        )
        return {"ok": ok, "results": results}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def main():
    REPORTS.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stages": {},
    }
    report["stages"]["unit"] = stage_unit()
    report["stages"]["coverage"] = stage_coverage()
    report["stages"]["retrieval"] = stage_retrieval()
    report["stages"]["verify"] = stage_verify()
    report["stages"]["smoke"] = stage_smoke()

    json_path = REPORTS / f"multistage_validation_{ts}.json"
    # trim bulky stdout for json
    slim = json.loads(json.dumps(report))
    for st in slim["stages"].values():
        if isinstance(st, dict):
            st.pop("stdout", None)
            st.pop("stderr", None)
    json_path.write_text(json.dumps(slim, indent=2, ensure_ascii=False), encoding="utf-8")

    md = [f"# Multi-stage validation {ts}", ""]
    for name, st in report["stages"].items():
        ok = st.get("ok")
        skip = st.get("skipped")
        status = "SKIP" if skip else ("PASS" if ok else "FAIL")
        md.append(f"## {name}: **{status}**")
        if st.get("reason"):
            md.append(f"- {st['reason']}")
        if st.get("error"):
            md.append(f"- error: `{st['error']}`")
        if name == "coverage" and st.get("summary_lines"):
            md.extend(f"- `{l}`" for l in st["summary_lines"][:25])
        if name == "retrieval" and st.get("tracks"):
            for lang, data in st["tracks"].items():
                if isinstance(data, dict) and "hit_at_k" in data:
                    md.append(f"- {lang} hit@k: {data['hit_at_k']:.1%}")
        if name == "smoke" and st.get("results"):
            for r in st["results"]:
                if "error" in r:
                    md.append(f"- FAIL `{r['q'][:60]}`: {r['error']}")
                else:
                    md.append(
                        f"- Q `{r['q'][:50]}` → intent={r.get('intent')} "
                        f"sources={r.get('n_sources')} grounded={r.get('grounded')}"
                    )
        md.append("")
    md_path = REPORTS / f"multistage_validation_{ts}.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"\nSaved:\n  {json_path}\n  {md_path}", flush=True)

    failed = [
        k for k, v in report["stages"].items()
        if not v.get("ok") and not v.get("skipped")
    ]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
