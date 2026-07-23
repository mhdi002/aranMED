"""CKO + Knowledge Graph schema helpers (SQLite in catalog.db)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def init_knowledge_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cko (
            id TEXT PRIMARY KEY,
            title TEXT,
            clinical_question TEXT,
            answer_summary TEXT,
            evidence TEXT,
            source_doc TEXT,
            version TEXT,
            year INTEGER,
            country TEXT,
            specialty TEXT,
            active INTEGER DEFAULT 1,
            relations_json TEXT,
            chunk_ids_json TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS kg_nodes (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            label TEXT NOT NULL,
            synonyms TEXT,
            ontology_code TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS kg_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            src TEXT NOT NULL,
            rel TEXT NOT NULL,
            dst TEXT NOT NULL,
            evidence_cko_id TEXT,
            source_chunk_id TEXT,
            confidence REAL DEFAULT 0.5,
            created_at TEXT,
            UNIQUE(src, rel, dst)
        );
    """)
    # Versioning columns on documents
    cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
    for col, typ in (("edition", "TEXT"), ("year", "INTEGER"), ("supersedes", "TEXT"),
                     ("country", "TEXT"), ("active", "INTEGER")):
        if col not in cols:
            try:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
    conn.commit()


def upsert_cko(conn: sqlite3.Connection, cko: dict):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO cko "
        "(id, title, clinical_question, answer_summary, evidence, source_doc, version, "
        "year, country, specialty, active, relations_json, chunk_ids_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cko["id"], cko.get("title"), cko.get("clinical_question"),
            cko.get("answer_summary"), cko.get("evidence"), cko.get("source_doc"),
            cko.get("version"), cko.get("year"), cko.get("country"),
            cko.get("specialty"), 1 if cko.get("active", True) else 0,
            json.dumps(cko.get("relations") or [], ensure_ascii=False),
            json.dumps(cko.get("chunk_ids") or [], ensure_ascii=False),
            now,
        ),
    )
    conn.commit()


def upsert_node(conn: sqlite3.Connection, node_id: str, ntype: str, label: str,
                synonyms: list[str] | None = None, ontology_code: str | None = None):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO kg_nodes (id, type, label, synonyms, ontology_code, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (node_id, ntype, label, json.dumps(synonyms or [], ensure_ascii=False),
         ontology_code, now),
    )
    conn.commit()


def upsert_edge(conn: sqlite3.Connection, src: str, rel: str, dst: str,
                evidence_cko_id: str | None = None, source_chunk_id: str | None = None,
                confidence: float = 0.5):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO kg_edges "
        "(src, rel, dst, evidence_cko_id, source_chunk_id, confidence, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (src, rel, dst, evidence_cko_id, source_chunk_id, confidence, now),
    )
    conn.commit()


def node_id(ntype: str, label: str) -> str:
    slug = "".join(c if c.isalnum() or c in "_\u0600-\u06FF" else "_" for c in label.lower())
    slug = "_".join(filter(None, slug.split("_")))[:60]
    return f"{ntype}:{slug}"


def expand_one_hop(conn: sqlite3.Connection, query: str, limit: int = 8) -> list[dict]:
    """Find KG edges whose node labels appear in the query."""
    q = query.lower()
    nodes = conn.execute("SELECT id, type, label FROM kg_nodes").fetchall()
    matched = []
    for nid, ntype, label in nodes:
        if label and label.lower() in q:
            matched.append(nid)
    if not matched:
        # token overlap
        tokens = set(q.split())
        for nid, ntype, label in nodes:
            lab = (label or "").lower()
            if any(t in lab for t in tokens if len(t) > 3):
                matched.append(nid)
    facts = []
    seen = set()
    for nid in matched:
        rows = conn.execute(
            "SELECT e.src, e.rel, e.dst, n1.label, n2.label, e.confidence "
            "FROM kg_edges e "
            "JOIN kg_nodes n1 ON n1.id=e.src "
            "JOIN kg_nodes n2 ON n2.id=e.dst "
            "WHERE e.src=? OR e.dst=? "
            "ORDER BY e.confidence DESC LIMIT ?",
            (nid, nid, limit),
        ).fetchall()
        for src, rel, dst, sl, dl, conf in rows:
            key = (src, rel, dst)
            if key in seen:
                continue
            seen.add(key)
            facts.append({
                "src": src, "rel": rel, "dst": dst,
                "src_label": sl, "dst_label": dl, "confidence": conf,
                "fact": f"{sl} —{rel}→ {dl}",
            })
            if len(facts) >= limit:
                return facts
    return facts
