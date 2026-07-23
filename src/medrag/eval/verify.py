"""LLM-judged RAG verification harness."""
from __future__ import annotations

import argparse
import json
import random
import re
import time
from datetime import datetime

from medrag.config import REPORTS_DIR
from medrag.index import vectorstore as vs
from medrag.rag import routing
from medrag.rag.engine import RagEngine

CURATED = [
    "What is the recommended first-line antibiotic regimen for community-acquired pneumonia in a hospitalized adult?",
    "In a patient with new-onset atrial fibrillation, how is stroke risk stratified and when is anticoagulation indicated?",
    "What are the diagnostic criteria for diabetic ketoacidosis and the initial fluid and insulin management?",
    "Describe the management of an acute ST-elevation myocardial infarction including reperfusion time targets.",
    "What are the indications for and target ranges of preoperative fasting before elective surgery?",
    "How is severe sepsis/septic shock managed in the first hour according to guidelines?",
    "What is the stepwise pharmacologic management of chronic heart failure with reduced ejection fraction?",
    "What are the red-flag features of headache that warrant urgent neuroimaging?",
]


def parse_json(text):
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def sample_chunks(n, eng: RagEngine):
    points, _ = vs.scroll(limit=2000, with_payload=True)
    random.shuffle(points)
    picked = [p for p in points if len(p.payload.get("text", "")) > 400]
    return picked[:n]


def gen_question(chunk_text):
    prompt = (
        "Write ONE hard clinical question answerable from the passage. "
        'Return JSON {"question": "...", "answer": "..."}.\n\nPassage:\n' + chunk_text[:1800])
    out = routing.ollama_chat([{"role": "user", "content": prompt}], fmt="json", temperature=0.2)
    return parse_json(out)


def judge(question, gold, generated):
    prompt = (
        'Rate CANDIDATE vs REFERENCE. Return JSON {"faithful": 0-1, "relevant": 0-1, "correct": 0-1}.\n\n'
        f"Question: {question}\nReference: {gold}\nCandidate: {generated}")
    try:
        out = routing.ollama_chat([{"role": "user", "content": prompt}], fmt="json", temperature=0)
        d = parse_json(out)
        return {k: float(d.get(k, 0)) for k in ("faithful", "relevant", "correct")}
    except Exception:
        return {"faithful": 0, "relevant": 0, "correct": 0}


def eval_auto(n, eng: RagEngine):
    chunks = sample_chunks(n, eng)
    results, hit, mrr_sum = [], 0, 0.0
    for i, pt in enumerate(chunks, 1):
        src_title = pt.payload["title"]
        try:
            qa = gen_question(pt.payload["text"])
        except Exception as e:
            print(f"  [{i}] question-gen failed: {e}")
            continue
        q = qa.get("question", "").strip()
        if not q:
            continue
        passages = eng.retrieve(q, final_k=8)
        titles = [p.get("title", "") for p in passages]
        rank = next((r for r, t in enumerate(titles) if t == src_title), None)
        if rank is not None:
            hit += 1
            mrr_sum += 1.0 / (rank + 1)
        res = eng.answer(q)
        scores = judge(q, qa.get("answer", ""), res["answer"])
        results.append({"question": q, "source": src_title, "retrieved_rank": rank, **scores})
        print(f"  [{i}/{len(chunks)}] rank={rank} correct={scores['correct']:.1f}")
    n_eval = len(results)
    return {
        "n": n_eval,
        "hit_rate@8": round(hit / n_eval, 3) if n_eval else 0,
        "mrr@8": round(mrr_sum / n_eval, 3) if n_eval else 0,
        "avg_faithful": round(sum(r["faithful"] for r in results) / n_eval, 3) if n_eval else 0,
        "avg_relevant": round(sum(r["relevant"] for r in results) / n_eval, 3) if n_eval else 0,
        "avg_correct": round(sum(r["correct"] for r in results) / n_eval, 3) if n_eval else 0,
    }, results


def eval_curated(eng: RagEngine):
    out = []
    for q in CURATED:
        res = eng.answer(q)
        has_src = bool(res["sources"])
        out.append({
            "question": q, "answer": res["answer"],
            "specialties": res.get("specialties"),
            "has_sources": has_src,
            "sources": [f"{s['title']} p.{s['page']}" for s in res["sources"][:3]],
        })
        print(f"  answered ({'ok' if has_src else 'NO SOURCES'}): {q[:60]}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--curated-only", action="store_true")
    args = ap.parse_args()

    eng = RagEngine()
    print(f"Qdrant chunks: {vs.count()}")
    report = {"timestamp": datetime.now().isoformat(), "chunk_count": vs.count()}

    if not args.curated_only:
        print("\n== Auto-generated QA ==")
        summary, details = eval_auto(args.n, eng)
        report["auto"] = summary
        report["auto_details"] = details

    print("\n== Curated hard questions ==")
    report["curated"] = eval_curated(eng)

    ts = time.strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"verification_{ts}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nReport: {path}")


if __name__ == "__main__":
    main()
