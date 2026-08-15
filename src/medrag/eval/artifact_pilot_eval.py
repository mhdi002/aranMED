"""Knowledge Artifact pilot evaluation — docs/core/KNOWLEDGE_ARTIFACT_SCHEMA_v1.md §6.

Compares the pilot's classification results (catalog.db::knowledge_artifacts,
written by :mod:`medrag.knowledge.pilot_sample`) against a small
human-labeled gold set, and writes a PASS/FAIL-style markdown report in the
same spirit as reports/full_system_verify.md. This is a *required gate*:
docs/core/KNOWLEDGE_ARTIFACT_SCHEMA_v1.md explicitly says no code in this
codebase scales classification past the ~1000-chunk pilot without a human
reviewing this report first.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from medrag.config import CATALOG_DB, EVAL_DIR, REPORTS_DIR
from medrag.knowledge.artifact_schema import ARTIFACT_TYPES

GOLD_PATH = EVAL_DIR / "artifact_gold.jsonl"
REPORT_PATH = REPORTS_DIR / "artifact_pilot_eval.md"


def write_labeling_template(*, n: int = 100, db_path: Path | str | None = None,
                            out_path: Path = GOLD_PATH) -> int:
    """Sample *n* already-classified pilot chunks into a template a human
    fills in (``gold_type`` left blank) — the starting point for the gold
    set this eval reads. Returns the number of rows written.
    """
    conn = sqlite3.connect(db_path or CATALOG_DB)
    try:
        rows = conn.execute(
            "SELECT chunk_id, artifact_type, confidence, rationale, source_corpus, specialty "
            "FROM knowledge_artifacts ORDER BY RANDOM() LIMIT ?",
            (n,),
        ).fetchall()
    finally:
        conn.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for chunk_id, atype, conf, rationale, corpus, specialty in rows:
            f.write(
                json.dumps(
                    {
                        "chunk_id": chunk_id,
                        "predicted_type": atype,
                        "predicted_confidence": conf,
                        "predicted_rationale": rationale,
                        "source_corpus": corpus,
                        "specialty": specialty,
                        "gold_type": None,  # a human fills this in
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return len(rows)


def load_gold(path: Path = GOLD_PATH) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("gold_type"):
            out.append(row)
    return out


def _predictions_by_chunk(db_path: Path | str | None = None) -> dict[str, dict]:
    conn = sqlite3.connect(db_path or CATALOG_DB)
    try:
        rows = conn.execute(
            "SELECT chunk_id, artifact_type, confidence FROM knowledge_artifacts"
        ).fetchall()
    finally:
        conn.close()
    return {cid: {"type": t, "confidence": c} for cid, t, c in rows}


def compute_metrics(gold: list[dict], predictions: dict[str, dict]) -> dict:
    """Per-type precision/recall over the labeled subset."""
    tp: dict[str, int] = {t: 0 for t in ARTIFACT_TYPES}
    fp: dict[str, int] = {t: 0 for t in ARTIFACT_TYPES}
    fn: dict[str, int] = {t: 0 for t in ARTIFACT_TYPES}
    matched = 0
    for row in gold:
        pred = predictions.get(row["chunk_id"])
        if pred is None:
            continue
        matched += 1
        gold_type = row["gold_type"]
        pred_type = pred["type"]
        if pred_type == gold_type:
            tp[pred_type] = tp.get(pred_type, 0) + 1
        else:
            fp[pred_type] = fp.get(pred_type, 0) + 1
            fn[gold_type] = fn.get(gold_type, 0) + 1

    per_type = {}
    for t in ARTIFACT_TYPES:
        p_denom = tp[t] + fp[t]
        r_denom = tp[t] + fn[t]
        precision = tp[t] / p_denom if p_denom else None
        recall = tp[t] / r_denom if r_denom else None
        if p_denom or r_denom:
            per_type[t] = {"precision": precision, "recall": recall, "tp": tp[t], "fp": fp[t], "fn": fn[t]}
    overall_correct = sum(tp.values())
    accuracy = overall_correct / matched if matched else None
    return {"matched": matched, "gold_size": len(gold), "accuracy": accuracy, "per_type": per_type}


def render_report(metrics: dict) -> str:
    lines = [
        "# Knowledge Artifact Pilot — Evaluation",
        "",
        f"Gold-labeled chunks: {metrics['gold_size']} | Matched to a pilot prediction: {metrics['matched']}",
        "",
    ]
    if metrics["matched"] == 0:
        lines += [
            "**No labeled gold rows matched a pilot prediction.**",
            "",
            f"Run `write_labeling_template()` to generate {GOLD_PATH}, fill in "
            "`gold_type` for each row, then re-run this evaluation.",
        ]
        return "\n".join(lines) + "\n"

    lines.append(f"Overall accuracy: **{metrics['accuracy']:.1%}**" if metrics["accuracy"] is not None else "Overall accuracy: n/a")
    lines += ["", "| Type | Precision | Recall | TP | FP | FN |", "|---|---|---|---|---|---|"]
    for t, m in sorted(metrics["per_type"].items()):
        p = f"{m['precision']:.2f}" if m["precision"] is not None else "n/a"
        r = f"{m['recall']:.2f}" if m["recall"] is not None else "n/a"
        lines.append(f"| {t} | {p} | {r} | {m['tp']} | {m['fp']} | {m['fn']} |")
    lines += [
        "",
        "**Scale-up gate** (docs/core/KNOWLEDGE_ARTIFACT_SCHEMA_v1.md §6): "
        "going beyond this ~1000-chunk pilot to 10k/100k/446k chunks requires "
        "a human review of these numbers first — nothing in this codebase "
        "does that automatically.",
    ]
    return "\n".join(lines) + "\n"


def run_eval(*, db_path: Path | str | None = None, gold_path: Path = GOLD_PATH,
            report_path: Path = REPORT_PATH) -> dict:
    gold = load_gold(gold_path)
    predictions = _predictions_by_chunk(db_path)
    metrics = compute_metrics(gold, predictions)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(metrics), encoding="utf-8")
    return metrics


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--make-template", action="store_true",
                    help=f"Write a labeling template to {GOLD_PATH} and exit")
    ap.add_argument("--template-size", type=int, default=100)
    args = ap.parse_args()
    if args.make_template:
        n = write_labeling_template(n=args.template_size)
        print(f"wrote {n} rows to {GOLD_PATH} — fill in gold_type, then re-run without --make-template")
    else:
        result = run_eval()
        print(json.dumps(result, indent=2))
        print(f"report written to {REPORT_PATH}")
