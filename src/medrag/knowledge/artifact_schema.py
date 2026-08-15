"""Knowledge Artifact Schema v1 — docs/core/KNOWLEDGE_ARTIFACT_SCHEMA_v1.md.

Pure data shapes + the SQLite schema for the pilot's ``knowledge_artifacts``
table in ``catalog.db``. This table is purely additive: it is keyed by the
existing, stable ``chunk_id`` already in every Qdrant point's payload, and
nothing here ever writes back into Qdrant or touches the retrieval path.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

ARTIFACT_TYPES = (
    "FACT",
    "DEFINITION",
    "GUIDELINE",
    "PROTOCOL",
    "DRUG_INFORMATION",
    "SAFETY_INFORMATION",
    "REPORT_KNOWLEDGE",
    "RULE_CANDIDATE",
    "OTHER",
)


@dataclass
class ArtifactResult:
    chunk_id: str
    artifact_type: str
    confidence: float
    rationale: str
    classifier: str  # "heuristic_v1" | "llm_v1"
    source_corpus: str | None = None
    specialty: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def init_artifacts_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS knowledge_artifacts (
            chunk_id       TEXT PRIMARY KEY,
            artifact_type  TEXT NOT NULL,
            confidence     REAL NOT NULL,
            rationale      TEXT,
            classifier     TEXT NOT NULL,
            source_corpus  TEXT,
            specialty      TEXT,
            rule_candidate INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_artifacts_type ON knowledge_artifacts(artifact_type);
        CREATE INDEX IF NOT EXISTS ix_artifacts_rule_candidate
            ON knowledge_artifacts(rule_candidate);
        """
    )
    conn.commit()


def upsert_artifact(conn: sqlite3.Connection, result: ArtifactResult) -> None:
    if result.artifact_type not in ARTIFACT_TYPES:
        raise ValueError(f"unknown artifact_type: {result.artifact_type!r}")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO knowledge_artifacts
            (chunk_id, artifact_type, confidence, rationale, classifier,
             source_corpus, specialty, rule_candidate, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(chunk_id) DO UPDATE SET
            artifact_type=excluded.artifact_type,
            confidence=excluded.confidence,
            rationale=excluded.rationale,
            classifier=excluded.classifier,
            source_corpus=excluded.source_corpus,
            specialty=excluded.specialty,
            rule_candidate=excluded.rule_candidate,
            created_at=excluded.created_at
        """,
        (
            result.chunk_id,
            result.artifact_type,
            result.confidence,
            result.rationale,
            result.classifier,
            result.source_corpus,
            result.specialty,
            1 if result.artifact_type == "RULE_CANDIDATE" else 0,
            now,
        ),
    )
    conn.commit()


def get_artifact(conn: sqlite3.Connection, chunk_id: str) -> dict | None:
    cur = conn.execute(
        "SELECT * FROM knowledge_artifacts WHERE chunk_id=?", (chunk_id,)
    )
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def artifact_counts_by_type(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT artifact_type, COUNT(*) FROM knowledge_artifacts GROUP BY artifact_type"
    ).fetchall()
    return {t: n for t, n in rows}
