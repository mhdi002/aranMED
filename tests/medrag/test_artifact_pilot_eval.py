"""Tests for the Knowledge Artifact pilot evaluation gate
(docs/core/KNOWLEDGE_ARTIFACT_SCHEMA_v1.md §6)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from medrag.eval.artifact_pilot_eval import (
    compute_metrics,
    load_gold,
    render_report,
    run_eval,
    write_labeling_template,
)
from medrag.knowledge.artifact_schema import ArtifactResult, init_artifacts_schema, upsert_artifact


def _seed_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    init_artifacts_schema(conn)
    upsert_artifact(conn, ArtifactResult("c1", "DRUG_INFORMATION", 0.7, "", "heuristic_v1", None, None))
    upsert_artifact(conn, ArtifactResult("c2", "RULE_CANDIDATE", 0.6, "", "heuristic_v1", None, None))
    upsert_artifact(conn, ArtifactResult("c3", "GUIDELINE", 0.5, "", "heuristic_v1", None, None))
    conn.close()


def test_write_labeling_template_samples_from_db(tmp_path):
    db_path = tmp_path / "catalog.db"
    _seed_db(db_path)
    out_path = tmp_path / "gold.jsonl"
    n = write_labeling_template(n=10, db_path=db_path, out_path=out_path)
    assert n == 3
    rows = out_path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3
    import json

    first = json.loads(rows[0])
    assert first["gold_type"] is None
    assert "predicted_type" in first


def test_load_gold_skips_unlabeled_rows(tmp_path):
    gold_path = tmp_path / "gold.jsonl"
    gold_path.write_text(
        '{"chunk_id": "c1", "gold_type": "DRUG_INFORMATION"}\n'
        '{"chunk_id": "c2", "gold_type": null}\n',
        encoding="utf-8",
    )
    rows = load_gold(gold_path)
    assert len(rows) == 1
    assert rows[0]["chunk_id"] == "c1"


def test_compute_metrics_perfect_agreement():
    gold = [
        {"chunk_id": "c1", "gold_type": "DRUG_INFORMATION"},
        {"chunk_id": "c2", "gold_type": "RULE_CANDIDATE"},
    ]
    predictions = {
        "c1": {"type": "DRUG_INFORMATION", "confidence": 0.7},
        "c2": {"type": "RULE_CANDIDATE", "confidence": 0.6},
    }
    metrics = compute_metrics(gold, predictions)
    assert metrics["accuracy"] == 1.0
    assert metrics["per_type"]["DRUG_INFORMATION"]["precision"] == 1.0


def test_compute_metrics_disagreement_counted_as_fp_fn():
    gold = [{"chunk_id": "c1", "gold_type": "GUIDELINE"}]
    predictions = {"c1": {"type": "FACT", "confidence": 0.2}}
    metrics = compute_metrics(gold, predictions)
    assert metrics["accuracy"] == 0.0
    assert metrics["per_type"]["FACT"]["fp"] == 1
    assert metrics["per_type"]["GUIDELINE"]["fn"] == 1


def test_render_report_handles_empty_gold():
    report = render_report({"matched": 0, "gold_size": 0, "accuracy": None, "per_type": {}})
    assert "No labeled gold rows" in report


def test_render_report_includes_scale_up_gate_language():
    metrics = compute_metrics(
        [{"chunk_id": "c1", "gold_type": "FACT"}], {"c1": {"type": "FACT", "confidence": 0.2}}
    )
    report = render_report(metrics)
    assert "Scale-up gate" in report
    assert "446k" in report


def test_run_eval_writes_report_file(tmp_path):
    db_path = tmp_path / "catalog.db"
    _seed_db(db_path)
    gold_path = tmp_path / "gold.jsonl"
    gold_path.write_text('{"chunk_id": "c1", "gold_type": "DRUG_INFORMATION"}\n', encoding="utf-8")
    report_path = tmp_path / "artifact_pilot_eval.md"

    metrics = run_eval(db_path=db_path, gold_path=gold_path, report_path=report_path)
    assert metrics["matched"] == 1
    assert report_path.is_file()
    assert "DRUG_INFORMATION" in report_path.read_text(encoding="utf-8")
