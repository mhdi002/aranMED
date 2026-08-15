"""Knowledge Artifact classification — docs/core/KNOWLEDGE_ARTIFACT_SCHEMA_v1.md §5.

Two tiers:

* :func:`classify_heuristic` — zero-GPU, keyword/structure signals. Runs on
  every pilot chunk unconditionally.
* :func:`classify_llm` — optional, reuses the existing MedicalRAG LLM
  client. Only invoked when explicitly requested (GPU-cost decision made at
  call time, not hardcoded here — see the schema doc §5).

This is a *classifier*, not the Rule Engine — the keyword lists below are
heuristic signals for a probabilistic guess, not clinical decision logic,
so unlike backend/rules/ they may live as code rather than externally
authored data. Nothing here writes a verdict directly into
banks/*.json — the caller (:mod:`medrag.knowledge.pilot_sample`) is the one
that turns a ``RULE_CANDIDATE`` result into a ``status: "candidate"`` rule
entry, and only after this module reports it as an artifact.
"""
from __future__ import annotations

import re

from .artifact_schema import ARTIFACT_TYPES, ArtifactResult

_DRUG_RX = re.compile(
    r"\b(mg/kg|mg/day|mcg|dosage|dosing|contraindicated|renal\s+adjustment|"
    r"hepatic\s+adjustment|loading\s+dose|maintenance\s+dose|drug[- ]drug\s+interaction)\b",
    re.I,
)
_RULE_CANDIDATE_RX = re.compile(
    r"\b(must\s+not|should\s+not|is\s+contraindicated|is\s+indicated\s+when|"
    r"if\s+[^.]{3,60}\bthen\b|threshold\s+of|shall\s+not|do\s+not\s+exceed)\b",
    re.I,
)
_SAFETY_RX = re.compile(
    r"\b(warning|caution|critical\s+finding|life[- ]threatening|adverse\s+(event|reaction)|"
    r"black\s+box|immediately\s+notify)\b",
    re.I,
)
_DEFINITION_RX = re.compile(
    r"^\s*[A-Z][\w\s'-]{2,60}\s+(is|are)\s+(defined\s+as|a\s+type\s+of|characterized\s+by|referred\s+to\s+as)\b",
)
_GUIDELINE_RX = re.compile(
    r"\b(recommend(s|ed|ation)?|guideline|society\s+of|\b(ACR|WHO|NICE|AHA|ACC|ESC|IDSA|CDC|KDIGO|NCCN|ASCO|AAN|ACOG|AAP)\b)\b"
)
_PROTOCOL_RX = re.compile(r"\b(protocol|procedure|step\s+\d+|standard\s+operating)\b", re.I)
_REPORT_RX = re.compile(
    r"\b(findings?:|impression:|template|is\s+normal\s+in\s+size|no\s+sign\s+of\s+(solid|cystic)\s+lesion)\b",
    re.I,
)

_DOC_TYPE_PRIOR = {
    "guideline": ("GUIDELINE", 0.4),
    "standard": ("PROTOCOL", 0.35),
    "legal": ("OTHER", 0.3),
    "textbook": ("FACT", 0.25),
}


def classify_heuristic(chunk: dict) -> ArtifactResult:
    """*chunk* is a Qdrant point payload (or a payload-shaped dict) —
    see docs/core/KNOWLEDGE_ARTIFACT_SCHEMA_v1.md §1 for the field list.
    """
    text = (chunk.get("text") or "")[:2000]
    chunk_id = chunk.get("chunk_id") or chunk.get("id") or ""
    source_corpus = chunk.get("source_corpus")
    specialty = chunk.get("specialty")

    # Highest-confidence, most specific signals first.
    if _DRUG_RX.search(text):
        return ArtifactResult(
            chunk_id, "DRUG_INFORMATION", 0.7,
            "matched drug dosing/interaction keywords", "heuristic_v1",
            source_corpus, specialty,
        )
    if _RULE_CANDIDATE_RX.search(text):
        return ArtifactResult(
            chunk_id, "RULE_CANDIDATE", 0.6,
            "matched imperative/conditional clinical-logic phrasing", "heuristic_v1",
            source_corpus, specialty,
        )
    if _SAFETY_RX.search(text):
        return ArtifactResult(
            chunk_id, "SAFETY_INFORMATION", 0.65,
            "matched safety/warning keywords", "heuristic_v1",
            source_corpus, specialty,
        )
    if _REPORT_RX.search(text) or "template" in (chunk.get("title") or "").lower():
        return ArtifactResult(
            chunk_id, "REPORT_KNOWLEDGE", 0.55,
            "matched report-structure/template phrasing", "heuristic_v1",
            source_corpus, specialty,
        )
    if _DEFINITION_RX.search(text):
        return ArtifactResult(
            chunk_id, "DEFINITION", 0.5,
            "matched definitional sentence structure", "heuristic_v1",
            source_corpus, specialty,
        )
    if _GUIDELINE_RX.search(text):
        return ArtifactResult(
            chunk_id, "GUIDELINE", 0.5,
            "matched guideline/society-recommendation keywords", "heuristic_v1",
            source_corpus, specialty,
        )
    if _PROTOCOL_RX.search(text):
        return ArtifactResult(
            chunk_id, "PROTOCOL", 0.45,
            "matched protocol/procedure keywords", "heuristic_v1",
            source_corpus, specialty,
        )

    doc_type = (chunk.get("doc_type") or "").lower()
    if doc_type in _DOC_TYPE_PRIOR:
        atype, conf = _DOC_TYPE_PRIOR[doc_type]
        return ArtifactResult(
            chunk_id, atype, conf,
            f"no keyword match; fell back to doc_type={doc_type!r} prior",
            "heuristic_v1", source_corpus, specialty,
        )

    if len(text.strip()) > 40:
        return ArtifactResult(
            chunk_id, "FACT", 0.2,
            "no strong signal; long declarative text defaulted to FACT",
            "heuristic_v1", source_corpus, specialty,
        )
    return ArtifactResult(
        chunk_id, "OTHER", 0.15,
        "no classifiable signal", "heuristic_v1", source_corpus, specialty,
    )


_LLM_PROMPT = """Classify the following medical text chunk into exactly one
of these types: FACT, DEFINITION, GUIDELINE, PROTOCOL, DRUG_INFORMATION,
SAFETY_INFORMATION, REPORT_KNOWLEDGE, RULE_CANDIDATE, OTHER.

RULE_CANDIDATE means the text reads as an executable clinical rule (an
"if X then Y" statement, a numeric threshold, a contraindication) that a
human has not yet reviewed for inclusion in a rule engine.

Reply with ONLY compact JSON: {{"type": "...", "confidence": 0.0-1.0, "rationale": "one short sentence"}}

Text:
{text}
"""


async def classify_llm(chunk: dict, *, medrag_client) -> ArtifactResult:
    """Opt-in LLM tier — see module docstring. *medrag_client* is any object
    exposing an ``ask(query, specialty=...)`` coroutine, e.g.
    :class:`medrag.integrations`-style clients or the RAG engine's own LLM
    chat function; kept generic so callers choose what to reuse."""
    import json as _json

    text = (chunk.get("text") or "")[:2000]
    chunk_id = chunk.get("chunk_id") or chunk.get("id") or ""
    heuristic = classify_heuristic(chunk)
    try:
        raw = await medrag_client.ask(_LLM_PROMPT.format(text=text), specialty=chunk.get("specialty"))
        answer = raw.get("answer") if isinstance(raw, dict) else str(raw)
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (answer or "").strip(), flags=re.I | re.M)
        parsed = _json.loads(cleaned)
        atype = str(parsed.get("type", "")).upper()
        if atype not in ARTIFACT_TYPES:
            return heuristic
        return ArtifactResult(
            chunk_id, atype, float(parsed.get("confidence", 0.5)),
            str(parsed.get("rationale", ""))[:200], "llm_v1",
            chunk.get("source_corpus"), chunk.get("specialty"),
        )
    except Exception:  # noqa: BLE001
        return heuristic
