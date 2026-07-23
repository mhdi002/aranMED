"""Detailed QA retrieval evaluation + metric plots.

Captures for each gold / smoke / verify question:
  - question text
  - retrieved sources (title, page, score, corpus, snippet)
  - answer (when LLM available)
  - metrics (hit, grounding, …)

Writes:
  reports/QA_RETRIEVAL_DETAIL_<ts>.{md,json}
  reports/plots/*.png
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
PLOTS = REPORTS / "plots"
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

SNIPPET = 360
SMOKE_QUESTIONS = [
    "What are ECG findings in hyperkalemia?",
    "Metformin starting dose with eGFR 25?",
    "Warfarin interacts with amiodarone — what should I watch?",
    "طبق استاندارد آزمایش تعیین مقاومت داروئی مایکوباکتریوم چه اصولی مهم است؟",
]


def _ensure_qdrant() -> dict:
    try:
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:6333/collections", timeout=3)
        return {"ok": True, "started": False}
    except Exception:
        ps1 = ROOT / "scripts" / "start_qdrant.ps1"
        r = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        try:
            import urllib.request
            urllib.request.urlopen("http://127.0.0.1:6333/collections", timeout=5)
            return {"ok": True, "started": True, "rc": r.returncode,
                    "stdout": (r.stdout or "")[-500:]}
        except Exception as e:
            return {"ok": False, "error": repr(e), "stderr": (r.stderr or "")[-500:]}


def _llm_status() -> dict:
    from medrag.config import (
        LLM_PROVIDER, LLM_MODEL, LLM_BASE_URL,
        LLM_FALLBACK_PROVIDER, LLM_FALLBACK_ENDPOINT, LLM_FALLBACK_MODEL,
        OLLAMA_URL,
    )
    import urllib.request
    status = {
        "provider": LLM_PROVIDER,
        "model": LLM_MODEL,
        "base_url": LLM_BASE_URL,
        "fallback_provider": LLM_FALLBACK_PROVIDER,
        "fallback_model": LLM_FALLBACK_MODEL,
        "primary_ok": False,
        "fallback_ok": False,
    }
    primary = (LLM_PROVIDER or "").lower()
    try:
        if primary == "ollama":
            ep = (OLLAMA_URL or "http://localhost:11434").rstrip("/")
            urllib.request.urlopen(f"{ep}/api/tags", timeout=3)
            status["primary_ok"] = True
        else:
            urllib.request.urlopen(f"{LLM_BASE_URL.rstrip('/')}/models", timeout=3)
            status["primary_ok"] = True
    except Exception as e:
        status["primary_error"] = str(e)
    if LLM_FALLBACK_PROVIDER:
        try:
            ep = (LLM_FALLBACK_ENDPOINT or OLLAMA_URL or "http://localhost:11434").rstrip("/")
            urllib.request.urlopen(f"{ep}/api/tags", timeout=3)
            status["fallback_ok"] = True
        except Exception as e:
            status["fallback_error"] = str(e)
    status["answer_capable"] = bool(status["primary_ok"] or status["fallback_ok"])
    return status


def _coverage() -> dict:
    from medrag.config import CATALOG_DB
    conn = sqlite3.connect(str(CATALOG_DB))
    rows = conn.execute(
        "SELECT source_corpus, COUNT(1), COALESCE(SUM(n_chunks),0) "
        "FROM documents GROUP BY source_corpus"
    ).fetchall()
    by = {c or "unknown": {"docs": int(d), "chunks": int(ch or 0)} for c, d, ch in rows}
    missing = 0
    try:
        miss_path = REPORTS / "en_library_missing.txt"
        if miss_path.exists():
            missing = sum(1 for ln in miss_path.read_text(encoding="utf-8").splitlines() if ln.strip())
    except OSError:
        pass
    conn.close()
    return {"by_corpus": by, "en_library_missing_titles": missing}


def _passage_brief(p: dict, rank: int) -> dict:
    text = (p.get("text") or p.get("display_text") or "")
    return {
        "rank": rank,
        "title": p.get("title") or p.get("book") or "?",
        "page": p.get("page", p.get("page_start")),
        "score": round(float(p.get("score") or 0), 4),
        "rerank_raw": round(float(p["rerank_raw"]), 4) if "rerank_raw" in p else None,
        "source_corpus": p.get("source_corpus"),
        "specialty": p.get("specialty"),
        "snippet": text[:SNIPPET].replace("\n", " ").strip(),
    }


def _sources_from_answer(res: dict) -> list[dict]:
    out = []
    for s in res.get("sources") or []:
        out.append({
            "n": s.get("n"),
            "title": s.get("title"),
            "page": s.get("page"),
            "score": s.get("score"),
            "source_corpus": s.get("source_corpus"),
            "specialty": s.get("specialty"),
        })
    return out


def _passthrough_rewrite(query: str, language: str = "en") -> dict:
    """Skip LLM query rewrite during gold retrieval (8GB VRAM: embedder + 9B LLM thrash)."""
    return {"original": query, "queries": [query], "hyde": None}


def _stabilize_retrieval_memory() -> dict:
    """Reduce VRAM spikes on 8GB GPUs during long eval runs."""
    import medrag.rag.retrieval as ret_mod
    import medrag.index.embedder as emb_mod
    notes = {}
    if getattr(ret_mod, "COLBERT_ENABLED", False):
        ret_mod.COLBERT_ENABLED = False
        notes["colbert"] = "disabled_for_eval"
    if hasattr(emb_mod, "COLBERT_ENABLED"):
        emb_mod.COLBERT_ENABLED = False
    # Prefer CE broaden threshold over fused scores when ColBERT off
    return notes


def _checkpoint(report: dict, ts: str) -> Path:
    path = REPORTS / f"QA_RETRIEVAL_DETAIL_{ts}.partial.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def run_retrieval(eng, report: dict | None = None, ts: str | None = None) -> dict:
    from medrag.config import EVAL_DIR
    from medrag.eval import retrieval_eval
    from medrag.rag import query as query_mod

    mem_notes = _stabilize_retrieval_memory()
    # Gold hit@k should measure dense/rerank retrieval, not LLM rewrite latency.
    orig_rewrite = query_mod.rewrite_queries
    query_mod.rewrite_queries = _passthrough_rewrite
    tracks = {}
    try:
        for label, name in (("EN", "gold_questions.jsonl"), ("FA", "gold_questions_fa.jsonl")):
            path = EVAL_DIR / name
            if not path.exists():
                tracks[label.lower()] = {"ok": False, "error": f"missing {path}"}
                continue
            print(f"\n=== RETRIEVAL {label} (no LLM rewrite, ColBERT off) ===", flush=True)
            tracks[label.lower()] = retrieval_eval.run_eval(path, label, eng=eng)
            if report is not None and ts:
                report["retrieval"] = {"ok": True, "tracks": tracks, **mem_notes}
                _checkpoint(report, ts)
                print(f"  checkpointed after {label}", flush=True)
    finally:
        query_mod.rewrite_queries = orig_rewrite
    return {"ok": True, "tracks": tracks, "query_rewrite": False, **mem_notes}


def run_verify_curated(eng, llm_ok: bool, report: dict | None = None, ts: str | None = None) -> dict:
    from medrag.eval.verify import CURATED
    results = []
    print("\n=== VERIFY CURATED ===", flush=True)
    for q in CURATED:
        print(f"Q: {q[:70]}", flush=True)
        item = {"question": q}
        try:
            # Always capture retrieval even if answer fails
            ctx = eng.retrieve(q)
            item["retrieved"] = [_passage_brief(p, i + 1) for i, p in enumerate(ctx)]
            if llm_ok:
                _cuda_relax()
                # Single-pass generate from already-retrieved passages (avoid 2nd retrieve).
                from medrag.rag.grounding import verify_answer
                resp, gen_ctx = eng._generate(q, ctx)
                g = verify_answer(q, resp, gen_ctx)
                sources = [
                    {
                        "n": i + 1,
                        "title": p.get("title", p.get("book", "?")),
                        "page": p.get("page", p.get("page_start")),
                        "score": round(float(p.get("score") or 0), 3),
                        "source_corpus": p.get("source_corpus"),
                        "specialty": p.get("specialty"),
                    }
                    for i, p in enumerate(gen_ctx)
                ]
                item.update({
                    "answer": resp or "",
                    "intent": None,
                    "specialties": None,
                    "sources": sources,
                    "n_sources": len(sources),
                    "grounded": g.get("grounded"),
                    "has_citations": g.get("has_citations"),
                    "grounding": g,
                    "rule_alerts": 0,
                    "answer_mode": "generate_from_retrieve",
                })
            else:
                item["answer"] = None
                item["answer_gap"] = "LLM unavailable — retrieval-only"
                item["n_sources"] = len(item["retrieved"])
                item["sources"] = [
                    {"n": r["rank"], "title": r["title"], "page": r["page"],
                     "score": r["score"], "source_corpus": r["source_corpus"]}
                    for r in item["retrieved"][:5]
                ]
            print(
                f"  sources={item.get('n_sources')} grounded={item.get('grounded')}",
                flush=True,
            )
        except Exception as e:
            item["error"] = repr(e)
            item["traceback"] = traceback.format_exc()[-800:]
            print(f"  ERROR {e}", flush=True)
        results.append(item)
        if report is not None and ts:
            report["verify"] = {
                "ok": False, "n": len(CURATED), "n_done": len(results),
                "n_with_sources": sum(1 for r in results if (r.get("n_sources") or 0) > 0),
                "llm_answers": llm_ok, "results": results, "partial": True,
            }
            _checkpoint(report, ts)
    n_ok = sum(1 for r in results if "error" not in r and (r.get("n_sources") or 0) > 0)
    out = {
        "ok": n_ok == len(results),
        "n": len(results),
        "n_with_sources": n_ok,
        "llm_answers": llm_ok,
        "results": results,
    }
    return out


def _cuda_relax() -> None:
    """Best-effort free fragmented CUDA memory between heavy steps."""
    try:
        from medrag.index import embedder
        embedder.release_local_models()
    except Exception:
        pass
    try:
        import gc
        gc.collect()
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def run_smoke(eng, llm_ok: bool, report: dict | None = None, ts: str | None = None) -> dict:
    results = []
    print("\n=== SMOKE ===", flush=True)
    for q in SMOKE_QUESTIONS:
        print(f"Q: {q[:70]}", flush=True)
        item = {"question": q}
        try:
            ctx = eng.retrieve(q)
            item["retrieved"] = [_passage_brief(p, i + 1) for i, p in enumerate(ctx)]
            if llm_ok:
                _cuda_relax()
                # Single-pass generate from already-retrieved passages (avoid 2nd retrieve).
                from medrag.rag.grounding import verify_answer
                resp, gen_ctx = eng._generate(q, ctx)
                g = verify_answer(q, resp, gen_ctx)
                sources = [
                    {
                        "n": i + 1,
                        "title": p.get("title", p.get("book", "?")),
                        "page": p.get("page", p.get("page_start")),
                        "score": round(float(p.get("score") or 0), 3),
                        "source_corpus": p.get("source_corpus"),
                        "specialty": p.get("specialty"),
                    }
                    for i, p in enumerate(gen_ctx)
                ]
                item.update({
                    "answer": resp or "",
                    "intent": None,
                    "specialties": None,
                    "sources": sources,
                    "n_sources": len(sources),
                    "grounded": g.get("grounded"),
                    "has_citations": g.get("has_citations"),
                    "grounding": g,
                    "rule_alerts": 0,
                    "answer_mode": "generate_from_retrieve",
                })
            else:
                item["answer"] = None
                item["answer_gap"] = "LLM unavailable — retrieval-only"
                item["n_sources"] = len(item["retrieved"])
                item["sources"] = [
                    {"n": r["rank"], "title": r["title"], "page": r["page"],
                     "score": r["score"], "source_corpus": r["source_corpus"]}
                    for r in item["retrieved"][:5]
                ]
            print(
                f"  sources={item.get('n_sources')} grounded={item.get('grounded')}",
                flush=True,
            )
        except Exception as e:
            item["error"] = repr(e)
            print(f"  ERROR {e}", flush=True)
        results.append(item)
        if report is not None and ts:
            report["smoke"] = {
                "ok": False, "n": len(SMOKE_QUESTIONS), "n_done": len(results),
                "n_with_sources": sum(1 for r in results if (r.get("n_sources") or 0) > 0),
                "llm_answers": llm_ok, "results": results, "partial": True,
            }
            _checkpoint(report, ts)
    n_ok = sum(1 for r in results if "error" not in r and (r.get("n_sources") or 0) > 0)
    return {
        "ok": n_ok == len(results),
        "n": len(results),
        "n_with_sources": n_ok,
        "llm_answers": llm_ok,
        "results": results,
    }


def _stage_status(report: dict) -> dict[str, str]:
    out = {}
    out["qdrant"] = "PASS" if report.get("qdrant", {}).get("ok") else "FAIL"
    out["coverage"] = "PASS" if report.get("coverage") else "FAIL"
    ret = report.get("retrieval") or {}
    tracks = (ret.get("tracks") or {})
    en = tracks.get("en") or {}
    fa = tracks.get("fa") or {}
    out["retrieval_en"] = (
        "PASS" if en.get("hit_at_k", 0) >= 0.8 else ("FAIL" if en else "SKIP")
    )
    out["retrieval_fa"] = (
        "PASS" if fa.get("hit_at_k", 0) >= 0.8 else ("FAIL" if fa else "SKIP")
    )
    ver = report.get("verify") or {}
    out["verify"] = "PASS" if ver.get("ok") else ("FAIL" if ver else "SKIP")
    sm = report.get("smoke") or {}
    out["smoke"] = "PASS" if sm.get("ok") else ("FAIL" if sm else "SKIP")
    llm = report.get("llm") or {}
    out["llm"] = "PASS" if llm.get("answer_capable") else "FAIL"
    return out


def make_plots(report: dict, ts: str) -> list[str]:
    PLOTS.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []

    # 1) hit@k EN vs FA
    tracks = ((report.get("retrieval") or {}).get("tracks") or {})
    labels, rates, colors = [], [], []
    for key, color in (("en", "#2a6f97"), ("fa", "#bc4749")):
        t = tracks.get(key) or {}
        if "hit_at_k" in t:
            labels.append(f"{key.upper()}\n({t.get('hits', 0)}/{t.get('total', 0)})")
            rates.append(100.0 * float(t["hit_at_k"]))
            colors.append(color)
    if labels:
        fig, ax = plt.subplots(figsize=(6, 4))
        bars = ax.bar(labels, rates, color=colors, width=0.55)
        ax.set_ylim(0, 110)
        ax.set_ylabel("Hit@k (%)")
        ax.set_title(f"Retrieval hit@k — {ts}")
        for b, v in zip(bars, rates):
            ax.text(b.get_x() + b.get_width() / 2, v + 2, f"{v:.0f}%",
                    ha="center", va="bottom", fontsize=11)
        ax.axhline(80, color="#888", ls="--", lw=0.8, label="80% threshold")
        ax.legend(loc="lower right")
        fig.tight_layout()
        p = PLOTS / f"hit_at_k_en_fa_{ts}.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        paths.append(str(p))

    # 2) grounding scores smoke + verify
    g_labels, g_vals, g_colors = [], [], []
    for section, color in (("smoke", "#40916c"), ("verify", "#7444a0")):
        for i, r in enumerate(((report.get(section) or {}).get("results") or []), 1):
            g = r.get("grounded")
            if g is None and isinstance(r.get("grounding"), dict):
                g = r["grounding"].get("grounded")
            if g is None:
                continue
            short = (r.get("question") or "")[:28]
            g_labels.append(f"{section[0].upper()}{i}: {short}")
            g_vals.append(float(g))
            g_colors.append(color)
    if g_vals:
        fig, ax = plt.subplots(figsize=(10, max(4, 0.35 * len(g_vals) + 1.5)))
        y = np.arange(len(g_vals))
        ax.barh(y, g_vals, color=g_colors, height=0.7)
        ax.set_yticks(y)
        ax.set_yticklabels(g_labels, fontsize=8)
        ax.set_xlim(0, 1.05)
        ax.set_xlabel("Grounding score")
        ax.set_title(f"Grounding — smoke & verify ({ts})")
        ax.axvline(0.35, color="#888", ls="--", lw=0.8, label="min 0.35")
        ax.legend(loc="lower right")
        fig.tight_layout()
        p = PLOTS / f"grounding_smoke_verify_{ts}.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        paths.append(str(p))

    # 3) pipeline stage pass/fail
    stages = _stage_status(report)
    if stages:
        order = list(stages.keys())
        vals = [1 if stages[k] == "PASS" else (0.5 if stages[k] == "SKIP" else 0)
                for k in order]
        cmap = {"PASS": "#2d6a4f", "FAIL": "#9b2226", "SKIP": "#adb5bd"}
        fig, ax = plt.subplots(figsize=(8, 4))
        bars = ax.bar(order, vals, color=[cmap[stages[k]] for k in order])
        ax.set_ylim(0, 1.25)
        ax.set_ylabel("Status (1=PASS)")
        ax.set_title(f"Pipeline stages — {ts}")
        ax.set_xticklabels(order, rotation=30, ha="right")
        for b, k in zip(bars, order):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.05,
                    stages[k], ha="center", fontsize=9)
        fig.tight_layout()
        p = PLOTS / f"pipeline_stages_{ts}.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        paths.append(str(p))

    # 4) coverage by corpus
    by = ((report.get("coverage") or {}).get("by_corpus") or {})
    if by:
        corpora = sorted(by.keys())
        docs = [by[c]["docs"] for c in corpora]
        chunks = [by[c]["chunks"] for c in corpora]
        x = np.arange(len(corpora))
        w = 0.38
        fig, ax = plt.subplots(figsize=(9, 4.5))
        ax.bar(x - w / 2, docs, w, label="docs", color="#1d3557")
        ax.bar(x + w / 2, [c / 1000 for c in chunks], w, label="chunks (×1000)",
               color="#457b9d")
        ax.set_xticks(x)
        ax.set_xticklabels(corpora, rotation=20, ha="right")
        ax.set_ylabel("Count")
        ax.set_title(f"Coverage by corpus — {ts}")
        ax.legend()
        fig.tight_layout()
        p = PLOTS / f"coverage_by_corpus_{ts}.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        paths.append(str(p))

    # 5) retrieval score distribution
    scores = []
    for key in ("en", "fa"):
        for d in ((tracks.get(key) or {}).get("details") or []):
            for r in d.get("retrieved") or []:
                if r.get("score") is not None:
                    scores.append(float(r["score"]))
    if scores:
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.hist(scores, bins=min(30, max(8, len(scores) // 5)),
                color="#264653", edgecolor="white")
        ax.set_xlabel("Retrieval score")
        ax.set_ylabel("Count")
        ax.set_title(f"Retrieval score distribution (gold) — {ts}")
        ax.axvline(float(np.mean(scores)), color="#e76f51", ls="--",
                   label=f"mean={np.mean(scores):.3f}")
        ax.legend()
        fig.tight_layout()
        p = PLOTS / f"retrieval_score_dist_{ts}.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        paths.append(str(p))

    return paths


def _md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def write_markdown(report: dict, path: Path, plot_paths: list[str]) -> None:
    ts = report["timestamp_local"]
    lines = [
        f"# QA Retrieval Detail — {ts}",
        "",
        f"- UTC: `{report['timestamp_utc']}`",
        f"- Qdrant points: `{report.get('qdrant_points')}`",
        f"- LLM: `{report.get('llm')}`",
        f"- Answer capable: **{report.get('llm', {}).get('answer_capable')}**",
        "",
        "## Stage status",
        "",
        "| Stage | Status |",
        "|-------|--------|",
    ]
    for k, v in _stage_status(report).items():
        lines.append(f"| {k} | **{v}** |")
    lines += ["", "## Plots", ""]
    for p in plot_paths:
        rel = Path(p).name
        lines.append(f"- `reports/plots/{rel}`")
        lines.append(f"  ![{rel}](plots/{rel})")
    lines.append("")

    cov = report.get("coverage") or {}
    lines += ["## Coverage", "", "| Corpus | Docs | Chunks |", "|--------|------|--------|"]
    for c, d in sorted((cov.get("by_corpus") or {}).items()):
        lines.append(f"| {c} | {d['docs']} | {d['chunks']} |")
    lines.append(f"\nEN library missing titles: {cov.get('en_library_missing_titles')}\n")

    # Retrieval gold tables
    tracks = ((report.get("retrieval") or {}).get("tracks") or {})
    for lang, data in tracks.items():
        if not isinstance(data, dict) or "details" not in data:
            continue
        lines += [
            f"## Retrieval gold — {lang.upper()}",
            "",
            f"hit@k = **{data.get('hit_at_k', 0):.1%}** "
            f"({data.get('hits')}/{data.get('total')}, k={data.get('k')})",
            "",
        ]
        for i, d in enumerate(data["details"], 1):
            mark = "HIT" if d.get("hit") else "MISS"
            lines.append(f"### {i}. [{mark}] {_md_escape(d['question'])}")
            lines.append("")
            lines.append(
                f"- matched keywords: `{d.get('matched_keywords')}`"
            )
            lines.append("")
            lines.append("| Rank | Title | Page | Score | Corpus | Snippet |")
            lines.append("|------|-------|------|-------|--------|---------|")
            for r in d.get("retrieved") or []:
                lines.append(
                    f"| {r.get('rank')} | {_md_escape(str(r.get('title')))} | "
                    f"{r.get('page')} | {r.get('score')} | {r.get('source_corpus')} | "
                    f"{_md_escape(str(r.get('snippet') or '')[:180])} |"
                )
            lines.append("")

    def _qa_section(name: str, block: dict) -> None:
        lines.append(f"## {name}")
        lines.append("")
        if not block:
            lines.append("_empty_")
            lines.append("")
            return
        lines.append(
            f"ok={block.get('ok')} with_sources={block.get('n_with_sources')}/"
            f"{block.get('n')} llm_answers={block.get('llm_answers')}"
        )
        lines.append("")
        for i, r in enumerate(block.get("results") or [], 1):
            lines.append(f"### {i}. {_md_escape(r.get('question', ''))}")
            lines.append("")
            if r.get("error"):
                lines.append(f"- **ERROR:** `{r['error']}`")
            if r.get("answer_gap"):
                lines.append(f"- _{r['answer_gap']}_")
            lines.append(
                f"- intent=`{r.get('intent')}` grounded=`{r.get('grounded')}` "
                f"citations=`{r.get('has_citations')}` sources=`{r.get('n_sources')}`"
            )
            lines.append("")
            lines.append("**Retrieved**")
            lines.append("")
            lines.append("| Rank | Title | Page | Score | Corpus | Snippet |")
            lines.append("|------|-------|------|-------|--------|---------|")
            for psg in r.get("retrieved") or []:
                lines.append(
                    f"| {psg.get('rank')} | {_md_escape(str(psg.get('title')))} | "
                    f"{psg.get('page')} | {psg.get('score')} | {psg.get('source_corpus')} | "
                    f"{_md_escape(str(psg.get('snippet') or '')[:180])} |"
                )
            lines.append("")
            ans = r.get("answer")
            if ans:
                lines.append("**Answer**")
                lines.append("")
                lines.append("```")
                lines.append(ans.strip())
                lines.append("```")
                lines.append("")
            srcs = r.get("sources") or []
            if srcs:
                lines.append("**Answer sources**")
                lines.append("")
                for s in srcs:
                    lines.append(
                        f"- [{s.get('n')}] {_md_escape(str(s.get('title')))} "
                        f"p.{s.get('page')} score={s.get('score')} "
                        f"({s.get('source_corpus')})"
                    )
                lines.append("")

    _qa_section("Verify curated", report.get("verify") or {})
    _qa_section("Smoke bilingual", report.get("smoke") or {})

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    PLOTS.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    log_path = REPORTS / f"_qa_eval_live_{ts}.log"

    class _Tee:
        def __init__(self, *streams):
            self.streams = streams
        def write(self, data):
            for s in self.streams:
                try:
                    s.write(data)
                    s.flush()
                except Exception:
                    pass
        def flush(self):
            for s in self.streams:
                try:
                    s.flush()
                except Exception:
                    pass

    log_f = open(log_path, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, log_f)
    sys.stderr = _Tee(sys.__stderr__, log_f)
    print(f"=== QA eval + plots @ {ts} ===", flush=True)
    print(f"live log: {log_path}", flush=True)

    report: dict = {
        "timestamp_local": ts,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

    qd = _ensure_qdrant()
    report["qdrant"] = qd
    if not qd.get("ok"):
        print(f"Qdrant failed: {qd}", flush=True)
        json_path = REPORTS / f"QA_RETRIEVAL_DETAIL_{ts}.json"
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    llm = _llm_status()
    report["llm"] = llm
    print(f"LLM status: {llm}", flush=True)
    llm_ok = bool(llm.get("answer_capable"))
    if not llm_ok:
        print("WARNING: no LLM — retrieval-only for verify/smoke", flush=True)

    from medrag.index import vectorstore as vs
    from medrag.rag.engine import RagEngine
    report["qdrant_points"] = vs.count()
    print(f"Qdrant points: {report['qdrant_points']}", flush=True)

    report["coverage"] = _coverage()
    print(f"Coverage: {report['coverage']}", flush=True)

    eng = RagEngine()
    # Self-RAG multiplies LLM calls; keep single-pass answers for this harness.
    import medrag.config as cfg_mod
    if getattr(cfg_mod, "SELF_RAG_ENABLED", False):
        cfg_mod.SELF_RAG_ENABLED = False
        report["self_rag"] = "disabled_for_eval"
    import medrag.rag.engine as engine_mod
    if hasattr(engine_mod, "SELF_RAG_ENABLED"):
        engine_mod.SELF_RAG_ENABLED = False

    resume = None
    for i, a in enumerate(sys.argv[1:]):
        if a == "--resume" and i + 2 <= len(sys.argv[1:]):
            resume = Path(sys.argv[1:][i + 1])
        elif a.startswith("--resume="):
            resume = Path(a.split("=", 1)[1])

    if resume and resume.exists():
        print(f"Resuming from {resume}", flush=True)
        prior = json.loads(resume.read_text(encoding="utf-8"))
        for k in ("retrieval", "coverage", "qdrant_points", "self_rag"):
            if k in prior and k not in report:
                report[k] = prior[k]
        if prior.get("retrieval"):
            report["retrieval"] = prior["retrieval"]
            print("  reused retrieval tracks from checkpoint", flush=True)
        else:
            report["retrieval"] = run_retrieval(eng, report=report, ts=ts)
    else:
        report["retrieval"] = run_retrieval(eng, report=report, ts=ts)
    _checkpoint(report, ts)

    report["verify"] = run_verify_curated(eng, llm_ok, report=report, ts=ts)
    _checkpoint(report, ts)

    report["smoke"] = run_smoke(eng, llm_ok, report=report, ts=ts)
    _checkpoint(report, ts)

    plot_paths = make_plots(report, ts)
    report["plots"] = plot_paths
    print(f"Plots ({len(plot_paths)}):", flush=True)
    for p in plot_paths:
        print(f"  {p}", flush=True)

    json_path = REPORTS / f"QA_RETRIEVAL_DETAIL_{ts}.json"
    md_path = REPORTS / f"QA_RETRIEVAL_DETAIL_{ts}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    write_markdown(report, md_path, plot_paths)
    # remove partial if final written
    partial = REPORTS / f"QA_RETRIEVAL_DETAIL_{ts}.partial.json"
    if partial.exists():
        try:
            partial.unlink()
        except OSError:
            pass
    print(f"\nSaved:\n  {json_path}\n  {md_path}", flush=True)

    stages = _stage_status(report)
    failed = [k for k, v in stages.items() if v == "FAIL" and k != "llm"]
    # LLM fail alone is not fatal if retrieval-only requested
    if stages.get("llm") == "FAIL":
        print("Note: LLM unavailable — answers may be missing.", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
