"""Pilot chunk sampling + orchestration — docs/core/KNOWLEDGE_ARTIFACT_SCHEMA_v1.md §4/§7.

Everything here is scoped to the ~1000-chunk pilot: sampling is payload-only
Qdrant scroll (no vectors fetched, cheap), classification is the heuristic
tier by default, and results land in a new ``catalog.db`` table — never in
Qdrant itself. Nothing in this module scales beyond the sample it's given;
scaling to the full 446k-chunk corpus is a future decision gated on the
pilot's eval report (see :mod:`medrag.eval.artifact_pilot_eval`).
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from medrag.config import CATALOG_DB
from medrag.index.vectorstore import STORE_NAMES, client, collection_name

from .artifact_schema import ArtifactResult, init_artifacts_schema, upsert_artifact
from .artifacts import classify_heuristic

RESULTS_DIR = Path(__file__).resolve().parents[3] / "results"
SAMPLE_FILE = RESULTS_DIR / "artifact_pilot_sample.json"
_DEFAULT_CLINICAL_BANK = (
    Path(__file__).resolve().parents[1] / "rules" / "banks" / "clinical.json"
)


def scroll_store_payloads(store: str, limit: int) -> list[dict]:
    """Payload-only Qdrant scroll for one logical store — no vectors fetched."""
    points, _next = client(store).scroll(
        collection_name(store), limit=limit, with_payload=True, with_vectors=False,
    )
    return [p.payload for p in points if p.payload]


def sample_chunk_ids(target: int = 1000, *, per_store: int | None = None) -> list[dict]:
    """Stratified sample across all 5 logical stores (schema doc §4).

    Returns full payload dicts (not bare ids) so classification runs
    directly against the sample without a second Qdrant round-trip. A store
    that is empty/unreachable is skipped rather than failing the whole
    pilot — see docs/core/KNOWLEDGE_ARTIFACT_SCHEMA_v1.md §4.
    """
    per_store = per_store or max(1, target // len(STORE_NAMES))
    sample: list[dict] = []
    for store in STORE_NAMES:
        try:
            sample.extend(scroll_store_payloads(store, per_store))
        except Exception:  # noqa: BLE001
            continue
    return sample[:target] if len(sample) > target else sample


def save_sample_manifest(sample: list[dict], path: Path = SAMPLE_FILE) -> None:
    """Persist the sampled chunk_ids for reproducibility/audit — schema §4."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ids = [c.get("chunk_id") for c in sample if c.get("chunk_id")]
    path.write_text(
        json.dumps({"count": len(ids), "chunk_ids": ids}, indent=2), encoding="utf-8"
    )


def classify_sample(
    sample: list[dict], *, classifier=classify_heuristic
) -> list[ArtifactResult]:
    return [classifier(c) for c in sample]


def persist_results(
    results: list[ArtifactResult], *, db_path: Path | str | None = None
) -> None:
    conn = sqlite3.connect(db_path or CATALOG_DB)
    try:
        init_artifacts_schema(conn)
        for r in results:
            if not r.chunk_id:
                continue
            upsert_artifact(conn, r)
    finally:
        conn.close()


def promote_rule_candidates(
    results: list[ArtifactResult],
    sample_by_id: dict[str, dict],
    *,
    min_confidence: float = 0.55,
    bank_path: Path | None = None,
) -> int:
    """Write RULE_CANDIDATE artifacts above *min_confidence* into the
    clinical rule bank as ``status: "candidate"`` entries — never
    ``approved``/``production``. See RULE_MODEL_SCHEMA_v1.md §5 and
    KNOWLEDGE_ARTIFACT_SCHEMA_v1.md §7. Returns the number newly written.
    Opt-in (never called by :func:`run_pilot` unless ``promote=True``) —
    the pilot's own confidence isn't itself a substitute for the human
    validation step the lifecycle requires before anything fires live.
    """
    bank_path = bank_path or _DEFAULT_CLINICAL_BANK
    candidates = [
        r
        for r in results
        if r.artifact_type == "RULE_CANDIDATE" and r.confidence >= min_confidence
    ]
    if not candidates:
        return 0
    data = (
        json.loads(bank_path.read_text(encoding="utf-8"))
        if bank_path.exists()
        else {"bank": "clinical", "rules": []}
    )
    data.setdefault("rules", [])
    existing_ids = {r["rule_id"] for r in data["rules"]}
    added = 0
    for r in candidates:
        rule_id = f"CANDIDATE-{r.chunk_id}"
        if rule_id in existing_ids:
            continue
        chunk = sample_by_id.get(r.chunk_id, {})
        snippet = (chunk.get("text") or "")[:200].strip()
        data["rules"].append(
            {
                "rule_id": rule_id,
                "bank": "clinical",
                "name": f"Candidate rule extracted from chunk {r.chunk_id}",
                "condition": {
                    "type": "regex",
                    "pattern": re.escape(snippet[:60]) or "zzz_never_matches",
                },
                "action": {"severity": "info", "code": rule_id, "message_en": snippet},
                "source": (
                    f"Knowledge Artifact pilot ({r.classifier}); "
                    f"chunk_id={r.chunk_id}; source_corpus={r.source_corpus}"
                ),
                "version": "0.1",
                "effective_from": None,
                "effective_to": None,
                "status": "candidate",
                "evidence_level": None,
                "validated_by": None,
                "dependencies": [],
                "tags": ["pilot", "unreviewed"],
            }
        )
        added += 1
    if added:
        bank_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    return added


def run_pilot(
    *, target: int = 1000, db_path: Path | str | None = None, promote: bool = False
) -> dict:
    """End-to-end pilot: sample -> classify -> persist -> (optionally) promote.

    Returns a small summary dict; the full precision/recall evaluation
    against a human-labeled gold set is a separate step — see
    :mod:`medrag.eval.artifact_pilot_eval`.
    """
    sample = sample_chunk_ids(target=target)
    results = classify_sample(sample)
    persist_results(results, db_path=db_path)
    save_sample_manifest(sample)
    promoted = 0
    if promote:
        by_id = {c.get("chunk_id"): c for c in sample if c.get("chunk_id")}
        promoted = promote_rule_candidates(results, by_id)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.artifact_type] = counts.get(r.artifact_type, 0) + 1
    return {"sampled": len(sample), "by_type": counts, "promoted_candidates": promoted}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", type=int, default=1000)
    ap.add_argument("--promote", action="store_true", help="Write RULE_CANDIDATE hits into banks/clinical.json")
    args = ap.parse_args()
    summary = run_pilot(target=args.target, promote=args.promote)
    print(json.dumps(summary, indent=2))
