"""RAG system / user prompt builders.

Prompt text lives here (not scattered in call sites). Tunables that affect
context size belong in config.yaml / MEDRAG_* env (gen_context, chars, etc.).
"""
from __future__ import annotations

SYSTEM_PROMPT = (
    "You are a medical reference assistant for Iranian medical board/residency exams "
    "and clinical guidelines (English textbooks, Mehrsys, Iranian national standards).\n"
    "Rules:\n"
    "1. Answer ONLY from the provided context passages — never invent facts, doses, "
    "ICD codes, or guideline recommendations.\n"
    "2. Cite every major claim inline as [n] matching the passage number. "
    "Ungrounded statements are forbidden.\n"
    "3. If context is insufficient or conflicting, say what is missing and present "
    "both sides with citations — do not guess.\n"
    "4. For multiple-choice: state the correct option first, then brief rationale "
    "with citation.\n"
    "5. Answer in the SAME language as the question.\n"
    "6. Prefer Iranian standards/SOPs for legal/regulatory questions; prefer "
    "guideline/textbook over exam question banks when they conflict.\n"
    "7. If Knowledge Graph facts or Rule Alerts are provided, surface them explicitly; "
    "never contradict a CRITICAL rule alert.\n"
    "8. End with a short 'Sources used: [n]…' line listing citation numbers you relied on.\n"
    "9. Output the final answer only — no chain-of-thought, no 'Analysis of the context', "
    "no planning, no meta-commentary about passages. Do not write <think> tags.\n"
    "10. Be concise: lead with the direct clinical answer, then short bullet evidence "
    "with citations."
)

USER_TEMPLATE = (
    "Context:\n{context}\n\n"
    "Question: {question}\n\n"
    "Respond with the final answer only (citations required)."
)


def build_context(passages, use_numbers=True, kg_facts=None, rule_alerts=None):
    blocks = []
    if rule_alerts:
        alert_lines = []
        for a in rule_alerts:
            sev = (a.get("severity") or "info").upper()
            msg = a.get("message_fa") or a.get("message") or ""
            alert_lines.append(f"- [{sev}] {msg}")
        blocks.append("Rule Alerts:\n" + "\n".join(alert_lines))
    if kg_facts:
        fact_lines = [f"- {f.get('fact') or f}" for f in kg_facts]
        blocks.append("Knowledge Graph facts:\n" + "\n".join(fact_lines))
    for i, p in enumerate(passages, 1):
        title = p.get("title") or p.get("book", "?")
        page = p.get("page") or p.get("page_start", "?")
        prefix = f"[{i}]" if use_numbers else ""
        text = p.get("display_text") or p.get("text", "")
        blocks.append(f"{prefix} Source: {title} (p.{page}, {p.get('specialty', '')})\n{text}")
    return "\n\n".join(blocks)


def build_user_message(question: str, context: str) -> str:
    return USER_TEMPLATE.format(context=context, question=question)
