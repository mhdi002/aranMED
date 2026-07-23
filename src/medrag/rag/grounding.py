"""Runtime answer grounding and faithfulness checks."""
from __future__ import annotations

import json
import re

from medrag.config import GROUNDING_CHECK, GROUNDING_MIN
from medrag.rag import routing

# Filler / CoT residue that should not count against lexical overlap
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "can", "may",
    "based", "provided", "context", "passage", "passages", "source", "sources",
    "according", "however", "therefore", "analysis", "question", "answer",
    "need", "must", "should", "also", "into", "over", "under", "about",
}


def _token_set(text: str) -> set[str]:
    return set(re.findall(r"[\w\u0600-\u06FF]{3,}", text.lower()))


def lexical_grounding_score(answer: str, context: str) -> float:
    """Fast overlap: fraction of answer content tokens found in context."""
    a_tok = _token_set(answer)
    c_tok = _token_set(context)
    if not a_tok:
        return 0.0
    a_tok -= _STOP
    if not a_tok:
        return 0.5
    return len(a_tok & c_tok) / len(a_tok)


def citation_coverage(answer: str, n_passages: int) -> float:
    """Fraction of cited [n] that fall in 1..n_passages (0 if no citations)."""
    nums = [int(x) for x in re.findall(r"\[(\d+)\]", answer or "")]
    if not nums or n_passages <= 0:
        return 0.0
    valid = [n for n in nums if 1 <= n <= n_passages]
    return len(valid) / len(nums)


def llm_grounding_check(question: str, answer: str, context: str) -> dict:
    prompt = (
        "You verify medical RAG answers. Rate if the answer is supported ONLY by the context.\n"
        'Return JSON {"grounded": 0-1, "issues": ["..."], "confidence": "high|medium|low"}\n\n'
        f"Question: {question}\n\nContext:\n{context[:6000]}\n\nAnswer:\n{answer[:3000]}"
    )
    try:
        raw = routing.ollama_chat([{"role": "user", "content": prompt}], fmt="json", temperature=0)
        data = json.loads(raw.strip())
        return {
            "grounded": float(data.get("grounded", 0)),
            "issues": data.get("issues", []),
            "confidence": data.get("confidence", "low"),
        }
    except Exception:
        return {"grounded": 0.0, "issues": ["grounding check failed"], "confidence": "low"}


def verify_answer(question: str, answer: str, passages: list[dict]) -> dict:
    if not GROUNDING_CHECK:
        return {"grounded": 1.0, "confidence": "high", "issues": [], "method": "disabled"}

    # Always score the cleaned final answer (ignore leaked CoT)
    answer = routing.strip_thinking(answer or "")
    context = "\n".join(
        p.get("display_text") or p.get("text", "") for p in passages
    )
    lex = lexical_grounding_score(answer, context)

    has_cite = bool(re.search(r"\[\d+\]", answer or ""))
    cite_cov = citation_coverage(answer, len(passages))
    cite_bonus = 0.08 if has_cite and cite_cov >= 0.8 else (0.04 if has_cite else -0.12)

    # Skip LLM judge when lexical score is clearly good (save latency / VRAM)
    if lex >= 0.60 and has_cite and cite_cov >= 0.8:
        return {
            "grounded": min(1.0, lex + cite_bonus),
            "confidence": "high",
            "issues": [],
            "method": "lexical",
            "has_citations": has_cite,
            "citation_coverage": round(cite_cov, 3),
        }

    llm = llm_grounding_check(question, answer, context)
    combined = 0.40 * lex + 0.50 * llm["grounded"] + (0.1 if has_cite else 0.0)
    conf = llm["confidence"]
    issues = list(llm.get("issues", []))
    if not has_cite:
        issues.append("missing inline citations [n]")
    elif cite_cov < 0.8:
        issues.append("invalid citation numbers outside retrieved passages")
    if combined < GROUNDING_MIN:
        conf = "low"
    return {
        "grounded": round(min(1.0, combined), 3),
        "confidence": conf,
        "issues": issues,
        "method": "hybrid",
        "has_citations": has_cite,
        "citation_coverage": round(cite_cov, 3),
    }


def insufficient_context_answer(language: str = "en") -> str:
    if language == "fa":
        return (
            "متأسفانه در منابع بازیابی‌شده اطلاعات کافی برای پاسخ دقیق یافت نشد. "
            "لطفاً سؤال را مشخص‌تر کنید یا تخصص مورد نظر را انتخاب کنید."
        )
    return (
        "The retrieved sources do not contain sufficient evidence to answer confidently. "
        "Please rephrase your question or specify the clinical context."
    )
