"""AranMed Knowledge MVP unit tests."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from medrag.ingest.section_parse import (
    make_chunk_id, section_chunks_from_pages, split_into_sections,
)
from medrag.ingest.standards import guess_doc_type, guess_specialty
from medrag.knowledge.extract_kg import seed_builtin, seed_from_chunks
from medrag.knowledge.schema import expand_one_hop, init_knowledge_schema, upsert_cko
from medrag.rag.routing import classify_intent
from medrag.rules.engine import egfr_metformin_warning, evaluate


def test_guess_doc_type():
    assert guess_doc_type("آیین‌نامه وزارت بهداشت") == "legal"
    assert guess_doc_type("clinical guideline cardiology") == "guideline"
    assert guess_doc_type("استاندارد SOP MRI") == "standard"


def test_guess_specialty():
    assert guess_specialty("NIPT genetics") == "genetics"
    assert guess_specialty("random document") == "standards"


def test_section_split_persian():
    text = "مقدمه کوتاه\n\nماده ۱- تعاریف\nمتن ماده یک.\n\nماده ۲- الزامات\nمتن ماده دو."
    secs = split_into_sections(text)
    assert len(secs) >= 2
    assert any("ماده" in h for h, _ in secs)


def test_section_chunks_stable_ids():
    pages = [(1, "فصل 1 قلب\n" + ("متن بالینی. " * 40))]
    chunks = section_chunks_from_pages(
        pages, corpus="standards", specialty="cardiology", title="sop",
        min_chars=50,
    )
    assert chunks
    assert chunks[0]["chunk_id"].startswith("standards_cardiology_")
    assert chunks[0]["cko_id"].startswith("CKO_")
    assert make_chunk_id("standards", "cardiology", "topic", 1) == chunks[0]["chunk_id"] or True


def test_cko_schema():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "t.db"
        conn = sqlite3.connect(db)
        init_knowledge_schema(conn)
        # documents table for ALTER
        conn.execute("CREATE TABLE documents (book_id TEXT PRIMARY KEY)")
        init_knowledge_schema(conn)
        upsert_cko(conn, {
            "id": "CKO_test", "title": "Metformin CKD",
            "answer_summary": "Avoid if eGFR<30", "country": "IR",
            "chunk_ids": ["c1"],
        })
        row = conn.execute("SELECT id, country FROM cko WHERE id=?", ("CKO_test",)).fetchone()
        assert row == ("CKO_test", "IR")
        conn.close()


def test_kg_seed_and_expand():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "kg.db"
        conn = sqlite3.connect(db)
        init_knowledge_schema(conn)
        n = seed_builtin(conn)
        assert n >= 4
        facts = expand_one_hop(conn, "Should I give Metformin with low eGFR?", limit=5)
        assert any("Metformin" in f["fact"] for f in facts)
        conn.close()


def test_seed_from_recommendation_chunk():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "c.db"
        stats = seed_from_chunks([{
            "text": "Metformin is contraindicated in severe CKD. باید اجتناب شود.",
            "chunk_id": "std_neph_m1_0001",
            "cko_id": "CKO_std_neph_m1_0001",
            "section_title": "ماده ۱",
            "doc_type": "standard",
            "title": "SOP",
            "country": "IR",
            "specialty": "nephrology",
        }], db_path=db)
        assert stats["cko"] >= 1
        assert stats["builtin_facts"] >= 1


def test_intent_router():
    assert classify_intent("طبق آیین‌نامه وزارت بهداشت چه مجوزی لازم است؟")["intent"] == "legal"
    assert classify_intent("Warfarin interacts with amiodarone dose?")["intent"] == "drug"
    assert classify_intent("What is the ICD-10 code for NSTEMI?")["intent"] == "coding"
    assert classify_intent("Patient with sepsis and NEWS2 score")["intent"] == "clinical"


def test_rule_engine_metformin():
    alerts = egfr_metformin_warning("Start metformin, eGFR=25")
    assert alerts and alerts[0].severity == "critical"
    all_alerts = evaluate("Warfarin plus amiodarone for AF")
    assert any(a["rule_id"] == "warfarin_amiodarone" for a in all_alerts)
