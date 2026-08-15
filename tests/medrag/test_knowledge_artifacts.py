"""Knowledge Artifact pilot tests — docs/core/KNOWLEDGE_ARTIFACT_SCHEMA_v1.md.

No live Qdrant/vLLM required: classification and storage are tested against
fixture chunk payloads and a temp SQLite file, matching the pattern used by
tests/medrag/test_knowledge_mvp.py for catalog.db-backed schemas. Only
:func:`medrag.knowledge.pilot_sample.scroll_store_payloads` talks to Qdrant,
and it is exercised here via monkeypatching, never a live connection.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from medrag.knowledge.artifact_schema import (
    ARTIFACT_TYPES,
    ArtifactResult,
    artifact_counts_by_type,
    get_artifact,
    init_artifacts_schema,
    upsert_artifact,
)
from medrag.knowledge.artifacts import classify_heuristic
from medrag.knowledge.pilot_sample import (
    classify_sample,
    persist_results,
    promote_rule_candidates,
    sample_chunk_ids,
    save_sample_manifest,
)

# ---------------------------------------------------------------------------
# Fixture chunk payloads — shaped like real Qdrant point payloads
# (src/medrag/index/build_index.py::upsert_chunks).
# ---------------------------------------------------------------------------
DRUG_CHUNK = {
    "chunk_id": "c_drug_1",
    "text": "Metformin dosing: contraindicated in renal impairment; maximum dose is 2000 mg/day.",
    "title": "Nephrology Formulary",
    "specialty": "nephrology",
    "doc_type": "textbook",
    "source_corpus": "library",
}
RULE_CANDIDATE_CHUNK = {
    "chunk_id": "c_rule_1",
    "text": (
        "If the patient's temperature exceeds 39C then initiate the sepsis "
        "protocol immediately; do not exceed the threshold of 3 consecutive "
        "abnormal readings without escalation."
    ),
    "title": "Anticoagulation Protocol",
    "specialty": "cardiology",
    "doc_type": "standard",
    "source_corpus": "standards",
}
SAFETY_CHUNK = {
    "chunk_id": "c_safety_1",
    "text": "Warning: this agent carries a black box warning for life-threatening hepatotoxicity.",
    "title": "Drug Safety Bulletin",
    "specialty": None,
    "doc_type": "guideline",
    "source_corpus": "standards",
}
DEFINITION_CHUNK = {
    "chunk_id": "c_def_1",
    "text": "Hydronephrosis is defined as dilation of the renal pelvis and calyces due to obstructed urine flow.",
    "title": "Radiology Glossary",
    "specialty": "radiology",
    "doc_type": "textbook",
    "source_corpus": "library",
}
GUIDELINE_CHUNK = {
    "chunk_id": "c_guide_1",
    "text": "The WHO recommends screening for hypertension in all adults over 40 years of age.",
    "title": "WHO Guideline",
    "specialty": "cardiology",
    "doc_type": "guideline",
    "source_corpus": "standards",
}
REPORT_CHUNK = {
    "chunk_id": "c_report_1",
    "text": "Thyroid Sonography: Both lobes are normal in size with no sign of solid or cystic lesion.",
    "title": "thyroid template",
    "specialty": "radiology",
    "doc_type": "textbook",
    "source_corpus": "library",
}
OTHER_CHUNK = {
    "chunk_id": "c_other_1",
    "text": "xyz",
    "title": "misc",
    "specialty": None,
    "doc_type": None,
    "source_corpus": "library",
}


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "chunk,expected_type",
    [
        (DRUG_CHUNK, "DRUG_INFORMATION"),
        (RULE_CANDIDATE_CHUNK, "RULE_CANDIDATE"),
        (SAFETY_CHUNK, "SAFETY_INFORMATION"),
        (DEFINITION_CHUNK, "DEFINITION"),
        (GUIDELINE_CHUNK, "GUIDELINE"),
        (REPORT_CHUNK, "REPORT_KNOWLEDGE"),
        (OTHER_CHUNK, "OTHER"),
    ],
)
def test_classify_heuristic(chunk, expected_type):
    result = classify_heuristic(chunk)
    assert result.artifact_type == expected_type
    assert result.artifact_type in ARTIFACT_TYPES
    assert result.classifier == "heuristic_v1"
    assert 0.0 <= result.confidence <= 1.0
    assert result.chunk_id == chunk["chunk_id"]


def test_classify_heuristic_never_crashes_on_empty_chunk():
    result = classify_heuristic({})
    assert result.artifact_type in ARTIFACT_TYPES


# ---------------------------------------------------------------------------
# Storage — additive table in catalog.db, keyed by chunk_id
# ---------------------------------------------------------------------------
def test_artifacts_schema_round_trip():
    with tempfile.TemporaryDirectory() as td:
        conn = sqlite3.connect(Path(td) / "catalog.db")
        init_artifacts_schema(conn)
        r = ArtifactResult("c1", "DRUG_INFORMATION", 0.7, "test", "heuristic_v1", "library", "nephrology")
        upsert_artifact(conn, r)
        fetched = get_artifact(conn, "c1")
        assert fetched["artifact_type"] == "DRUG_INFORMATION"
        assert fetched["rule_candidate"] == 0
        conn.close()


def test_rule_candidate_flag_set():
    with tempfile.TemporaryDirectory() as td:
        conn = sqlite3.connect(Path(td) / "catalog.db")
        init_artifacts_schema(conn)
        r = ArtifactResult("c2", "RULE_CANDIDATE", 0.6, "test", "heuristic_v1", None, None)
        upsert_artifact(conn, r)
        fetched = get_artifact(conn, "c2")
        assert fetched["rule_candidate"] == 1
        conn.close()


def test_upsert_is_idempotent_not_duplicated():
    with tempfile.TemporaryDirectory() as td:
        conn = sqlite3.connect(Path(td) / "catalog.db")
        init_artifacts_schema(conn)
        upsert_artifact(conn, ArtifactResult("c3", "FACT", 0.2, "a", "heuristic_v1", None, None))
        upsert_artifact(conn, ArtifactResult("c3", "GUIDELINE", 0.5, "b", "heuristic_v1", None, None))
        n = conn.execute("SELECT COUNT(*) FROM knowledge_artifacts WHERE chunk_id='c3'").fetchone()[0]
        assert n == 1
        assert get_artifact(conn, "c3")["artifact_type"] == "GUIDELINE"
        conn.close()


def test_invalid_artifact_type_rejected():
    with tempfile.TemporaryDirectory() as td:
        conn = sqlite3.connect(Path(td) / "catalog.db")
        init_artifacts_schema(conn)
        with pytest.raises(ValueError):
            upsert_artifact(conn, ArtifactResult("c4", "NOT_A_TYPE", 0.5, "x", "heuristic_v1", None, None))
        conn.close()


def test_artifact_counts_by_type():
    with tempfile.TemporaryDirectory() as td:
        conn = sqlite3.connect(Path(td) / "catalog.db")
        init_artifacts_schema(conn)
        upsert_artifact(conn, ArtifactResult("c5", "FACT", 0.2, "", "heuristic_v1", None, None))
        upsert_artifact(conn, ArtifactResult("c6", "FACT", 0.2, "", "heuristic_v1", None, None))
        upsert_artifact(conn, ArtifactResult("c7", "GUIDELINE", 0.5, "", "heuristic_v1", None, None))
        counts = artifact_counts_by_type(conn)
        assert counts["FACT"] == 2
        assert counts["GUIDELINE"] == 1
        conn.close()


# ---------------------------------------------------------------------------
# Pilot orchestration — Qdrant is mocked, never live
# ---------------------------------------------------------------------------
def test_sample_chunk_ids_stratifies_across_stores_and_degrades_gracefully():
    def fake_scroll(store, limit):
        if store == "main":
            raise RuntimeError("qdrant unreachable for this store in test")
        return [{"chunk_id": f"{store}_{i}", "text": "x" * 50} for i in range(3)]

    with patch("medrag.knowledge.pilot_sample.scroll_store_payloads", side_effect=fake_scroll):
        sample = sample_chunk_ids(target=100, per_store=3)

    # "main" failed but the pilot degrades gracefully rather than raising.
    stores_seen = {c["chunk_id"].split("_")[0] for c in sample}
    assert "main" not in stores_seen
    assert "standards" in stores_seen and "expand" in stores_seen


def test_sample_chunk_ids_caps_at_target():
    def fake_scroll(store, limit):
        return [{"chunk_id": f"{store}_{i}", "text": "x"} for i in range(limit)]

    with patch("medrag.knowledge.pilot_sample.scroll_store_payloads", side_effect=fake_scroll):
        sample = sample_chunk_ids(target=10, per_store=5)
    assert len(sample) <= 10


def test_classify_sample_and_persist_end_to_end(tmp_path):
    sample = [DRUG_CHUNK, RULE_CANDIDATE_CHUNK, REPORT_CHUNK]
    results = classify_sample(sample)
    assert {r.artifact_type for r in results} == {
        "DRUG_INFORMATION", "RULE_CANDIDATE", "REPORT_KNOWLEDGE",
    }
    db_path = tmp_path / "catalog.db"
    persist_results(results, db_path=db_path)
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM knowledge_artifacts").fetchone()[0]
    conn.close()
    assert n == 3


def test_save_sample_manifest_writes_reproducible_id_list(tmp_path):
    out = tmp_path / "manifest.json"
    save_sample_manifest([DRUG_CHUNK, RULE_CANDIDATE_CHUNK], path=out)
    import json

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["count"] == 2
    assert "c_drug_1" in data["chunk_ids"]


# ---------------------------------------------------------------------------
# Rule Engine hand-off — candidate rules only, never approved/production
# ---------------------------------------------------------------------------
def test_promote_rule_candidates_writes_candidate_status_only(tmp_path):
    bank_path = tmp_path / "clinical.json"
    results = classify_sample([RULE_CANDIDATE_CHUNK, DRUG_CHUNK])
    by_id = {c["chunk_id"]: c for c in [RULE_CANDIDATE_CHUNK, DRUG_CHUNK]}

    added = promote_rule_candidates(results, by_id, bank_path=bank_path)
    assert added == 1

    import json

    data = json.loads(bank_path.read_text(encoding="utf-8"))
    assert len(data["rules"]) == 1
    rule = data["rules"][0]
    assert rule["status"] == "candidate"
    assert rule["status"] not in ("approved", "production")
    assert rule["bank"] == "clinical"


def test_promote_rule_candidates_is_idempotent(tmp_path):
    bank_path = tmp_path / "clinical.json"
    results = classify_sample([RULE_CANDIDATE_CHUNK])
    by_id = {RULE_CANDIDATE_CHUNK["chunk_id"]: RULE_CANDIDATE_CHUNK}

    first = promote_rule_candidates(results, by_id, bank_path=bank_path)
    second = promote_rule_candidates(results, by_id, bank_path=bank_path)
    assert first == 1
    assert second == 0  # already present — not duplicated


def test_promote_rule_candidates_respects_confidence_threshold(tmp_path):
    bank_path = tmp_path / "clinical.json"
    results = classify_sample([RULE_CANDIDATE_CHUNK])
    by_id = {RULE_CANDIDATE_CHUNK["chunk_id"]: RULE_CANDIDATE_CHUNK}
    added = promote_rule_candidates(results, by_id, bank_path=bank_path, min_confidence=0.99)
    assert added == 0
