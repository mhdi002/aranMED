"""Seed knowledge graph + CKO rows from text patterns (rule + light heuristics)."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from medrag.catalog.documents import init_schema
from medrag.config import CATALOG_DB
from medrag.knowledge.schema import (
    init_knowledge_schema, node_id, upsert_cko, upsert_edge, upsert_node,
)

# Simple bilingual relation cues
REL_PATTERNS = [
    (r"(?P<a>[\w\u0600-\u06FF][\w\u0600-\u06FF\s-]{2,40}?)\s+(?:causes|منجر به|باعث)\s+(?P<b>[\w\u0600-\u06FF][\w\u0600-\u06FF\s-]{2,40})",
     "causes", "Disease", "Disease"),
    (r"(?P<a>[\w\u0600-\u06FF][\w\u0600-\u06FF\s-]{2,30}?)\s+(?:contraindicated in|ممنوع در|کنتراندیکه در)\s+(?P<b>[\w\u0600-\u06FF][\w\u0600-\u06FF\s-]{2,40})",
     "contraindicated_in", "Drug", "Disease"),
    (r"(?P<a>[\w\u0600-\u06FF][\w\u0600-\u06FF\s-]{2,30}?)\s+(?:interacts with|تداخل با)\s+(?P<b>[\w\u0600-\u06FF][\w\u0600-\u06FF\s-]{2,30})",
     "interacts_with", "Drug", "Drug"),
    (r"(?P<a>[\w\u0600-\u06FF][\w\u0600-\u06FF\s-]{2,40}?)\s+(?:requires|نیازمند|مستلزم)\s+(?P<b>[\w\u0600-\u06FF][\w\u0600-\u06FF\s-]{2,40})",
     "requires", "Disease", "Procedure"),
]

# Seed clinical facts (always present for rule engine demos)
SEED_FACTS = [
    ("Drug", "Metformin", "contraindicated_in", "Disease", "CKD eGFR<30", 0.95),
    ("Drug", "Warfarin", "interacts_with", "Drug", "Amiodarone", 0.9),
    ("Disease", "Diabetes", "causes", "Disease", "CKD", 0.85),
    ("Disease", "NSTEMI", "requires", "Procedure", "Dual Antiplatelet Therapy", 0.9),
    ("Disease", "AF CHA2DS2VASc>=2", "requires", "Drug", "Oral Anticoagulation", 0.9),
]


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" .،,;؛:-")[:80]


def seed_builtin(conn: sqlite3.Connection) -> int:
    n = 0
    for t1, a, rel, t2, b, conf in SEED_FACTS:
        na, nb = node_id(t1, a), node_id(t2, b)
        upsert_node(conn, na, t1, a)
        upsert_node(conn, nb, t2, b)
        upsert_edge(conn, na, rel, nb, confidence=conf)
        n += 1
    return n


def extract_from_text(conn: sqlite3.Connection, text: str, chunk_id: str | None = None,
                      cko_id: str | None = None, limit: int = 5) -> int:
    added = 0
    for pat, rel, t1, t2 in REL_PATTERNS:
        for m in re.finditer(pat, text, re.I):
            a, b = _clean(m.group("a")), _clean(m.group("b"))
            if len(a) < 3 or len(b) < 3:
                continue
            na, nb = node_id(t1, a), node_id(t2, b)
            upsert_node(conn, na, t1, a)
            upsert_node(conn, nb, t2, b)
            upsert_edge(conn, na, rel, nb, evidence_cko_id=cko_id,
                        source_chunk_id=chunk_id, confidence=0.55)
            added += 1
            if added >= limit:
                return added
    return added


def maybe_make_cko_from_chunk(conn: sqlite3.Connection, chunk: dict) -> str | None:
    """Create a CKO when text looks like a recommendation/standard clause."""
    text = chunk.get("text") or ""
    if not re.search(r"(باید|نباید|توصیه|shall|must|recommended|contraindicat|ممنوع)", text, re.I):
        return None
    cko_id = chunk.get("cko_id") or f"CKO_{chunk.get('chunk_id', 'x')}"
    upsert_cko(conn, {
        "id": cko_id,
        "title": chunk.get("section_title") or chunk.get("topic") or chunk.get("title"),
        "clinical_question": None,
        "answer_summary": text[:500],
        "evidence": chunk.get("doc_type"),
        "source_doc": chunk.get("title") or chunk.get("book"),
        "version": chunk.get("version"),
        "year": chunk.get("year"),
        "country": chunk.get("country") or "IR",
        "specialty": chunk.get("specialty"),
        "chunk_ids": [chunk.get("chunk_id")],
        "active": True,
    })
    return cko_id


def seed_from_chunks(chunks: list[dict], db_path: Path | None = None) -> dict:
    db_path = Path(db_path or CATALOG_DB)
    conn = sqlite3.connect(db_path)
    init_schema(conn)
    init_knowledge_schema(conn)
    builtin = seed_builtin(conn)
    edges = 0
    ckos = 0
    for ch in chunks:
        cko = maybe_make_cko_from_chunk(conn, ch)
        if cko:
            ckos += 1
        edges += extract_from_text(conn, ch.get("text", ""), ch.get("chunk_id"), cko)
    conn.close()
    return {"builtin_facts": builtin, "cko": ckos, "pattern_edges": edges}


def main():
    conn = sqlite3.connect(CATALOG_DB)
    init_schema(conn)
    init_knowledge_schema(conn)
    n = seed_builtin(conn)
    conn.close()
    print(f"Seeded {n} builtin KG facts into {CATALOG_DB}")


if __name__ == "__main__":
    main()
