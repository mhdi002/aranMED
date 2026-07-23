"""Self-RAG corrective loop: reflect → re-retrieve → regenerate."""
from __future__ import annotations

import json
import re

from medrag.config import RETRIEVAL, SELF_RAG_ENABLED, SELF_RAG_MAX_ITERS, SELF_RAG_THRESHOLD
from medrag.rag import routing
from medrag.rag.grounding import insufficient_context_answer, verify_answer


def _parse_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
    return {}


def reflect(query: str, answer: str, passages: list[dict], grounding: dict,
            iteration: int) -> dict:
    issues = grounding.get("issues") or []
    ctx_preview = "\n".join(
        (p.get("display_text") or p.get("text", ""))[:400] for p in passages[:3]
    )
    prompt = (
        "You are a Self-RAG controller for a medical QA system.\n"
        "Given the question, draft answer, grounding assessment, and retrieved snippets, "
        "decide the next action.\n"
        'Return JSON: {"action": "accept"|"retrieve"|"abstain", '
        '"search_queries": ["..."], "reason": "..."}\n'
        "- accept: answer is well supported\n"
        "- retrieve: need better/different sources — provide 1-3 new search queries "
        "(same language as question, medical terminology)\n"
        "- abstain: context cannot answer this question\n\n"
        f"Iteration: {iteration + 1}/{SELF_RAG_MAX_ITERS}\n"
        f"Grounding score: {grounding.get('grounded', 0)} ({grounding.get('confidence', '?')})\n"
        f"Issues: {issues}\n\n"
        f"Question: {query}\n\nDraft answer:\n{answer[:2000]}\n\n"
        f"Retrieved preview:\n{ctx_preview[:2500]}"
    )
    try:
        raw = routing.ollama_chat([{"role": "user", "content": prompt}], fmt="json", temperature=0)
        data = _parse_json(raw)
        action = str(data.get("action", "accept")).lower()
        if action not in ("accept", "retrieve", "abstain"):
            action = "retrieve" if grounding.get("grounded", 0) < SELF_RAG_THRESHOLD else "accept"
        queries = [q.strip() for q in data.get("search_queries", []) if q and q.strip()]
        return {"action": action, "search_queries": queries[:3], "reason": data.get("reason", "")}
    except Exception:
        if grounding.get("grounded", 0) < SELF_RAG_THRESHOLD:
            return {"action": "retrieve", "search_queries": [query], "reason": "fallback retrieve"}
        return {"action": "accept", "search_queries": [], "reason": "fallback accept"}


def should_correct(grounding: dict) -> bool:
    if not SELF_RAG_ENABLED:
        return False
    if grounding.get("has_citations") is False:
        return True
    if grounding.get("confidence") == "high" and grounding.get("grounded", 0) >= SELF_RAG_THRESHOLD:
        return False
    return grounding.get("grounded", 0) < SELF_RAG_THRESHOLD


def run_loop(query: str, lang: str, specs, retrieve_fn, generate_fn) -> dict:
    trace: list[dict] = []
    search_queries = None
    specialties = specs
    top_k_boost = 0

    for it in range(SELF_RAG_MAX_ITERS + 1):
        passages = retrieve_fn(
            query, specialties, lang,
            search_queries=search_queries,
            top_k_boost=top_k_boost,
        )
        if not passages:
            trace.append({"iter": it, "action": "retrieve", "reason": "empty results"})
            if it >= SELF_RAG_MAX_ITERS:
                break
            search_queries = [query]
            specialties = None
            top_k_boost = RETRIEVAL.get("top_k_search", 50) // 2
            continue

        answer, gen_ctx = generate_fn(query, passages)
        grounding = verify_answer(query, answer, gen_ctx)
        trace.append({
            "iter": it,
            "grounding": grounding.get("grounded"),
            "confidence": grounding.get("confidence"),
            "n_passages": len(passages),
        })

        if not should_correct(grounding):
            trace[-1]["final_action"] = "accept"
            return {
                "answer": answer,
                "passages": gen_ctx,
                "grounding": grounding,
                "self_rag": {"iterations": trace, "final_action": "accept"},
            }

        if it >= SELF_RAG_MAX_ITERS:
            reflection = reflect(query, answer, gen_ctx, grounding, it)
            trace[-1]["reflection"] = reflection
            if reflection["action"] == "abstain":
                return {
                    "answer": insufficient_context_answer(lang),
                    "passages": gen_ctx,
                    "grounding": grounding,
                    "self_rag": {"iterations": trace, "final_action": "abstain"},
                }
            trace[-1]["final_action"] = "accept_with_warning"
            return {
                "answer": answer,
                "passages": gen_ctx,
                "grounding": grounding,
                "self_rag": {"iterations": trace, "final_action": "accept_with_warning"},
            }

        reflection = reflect(query, answer, gen_ctx, grounding, it)
        trace[-1]["reflection"] = reflection

        if reflection["action"] == "abstain":
            return {
                "answer": insufficient_context_answer(lang),
                "passages": gen_ctx,
                "grounding": grounding,
                "self_rag": {"iterations": trace, "final_action": "abstain"},
            }
        if reflection["action"] == "accept":
            return {
                "answer": answer,
                "passages": gen_ctx,
                "grounding": grounding,
                "self_rag": {"iterations": trace, "final_action": "accept"},
            }

        search_queries = reflection["search_queries"] or [query]
        specialties = None
        top_k_boost = min(30, (it + 1) * 15)
        trace.append({
            "iter": it,
            "action": "corrective_retrieve",
            "queries": search_queries,
            "reason": reflection.get("reason", ""),
        })

    return {
        "answer": insufficient_context_answer(lang),
        "passages": [],
        "grounding": {"grounded": 0, "confidence": "low"},
        "self_rag": {"iterations": trace, "final_action": "abstain"},
    }
