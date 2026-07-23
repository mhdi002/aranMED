"""Unified RAG engine — Intent → Rules → Retriever(+KG) → Self-RAG."""
from __future__ import annotations

import sqlite3

from medrag.config import (
    CATALOG_DB, INTENT_ROUTING, KG_EXPANSION, KG_MAX_FACTS,
    PARENT_CHILD, RETRIEVAL, RULE_ENGINE, SELF_RAG_ENABLED,
    UNLOAD_LOCAL_BEFORE_GENERATE,
)
from medrag.index import vectorstore as vs
from medrag.ingest.contextual import gen_display_text
from medrag.rag import multimodal, prompts, routing
from medrag.rag.compression import compress_passages
from medrag.rag.grounding import insufficient_context_answer
from medrag.rag.query import decompose_if_complex, rewrite_queries
from medrag.rag import retrieval
from medrag.rag.self_rag import run_loop


class RagEngine:
    def __init__(self):
        vs.ensure_collection()
        try:
            vs.ensure_payload_indexes()
        except Exception:
            pass

    def _kg_facts(self, query: str) -> list[dict]:
        if not KG_EXPANSION:
            return []
        try:
            from medrag.knowledge.schema import expand_one_hop, init_knowledge_schema
            conn = sqlite3.connect(CATALOG_DB)
            init_knowledge_schema(conn)
            facts = expand_one_hop(conn, query, limit=KG_MAX_FACTS)
            conn.close()
            return facts
        except Exception:
            return []

    def _rule_alerts(self, query: str) -> list[dict]:
        if not RULE_ENGINE:
            return []
        try:
            from medrag.rules.engine import evaluate
            return evaluate(query)
        except Exception:
            return []

    def retrieve(self, query, specialties=None, language=None,
                 top_k=None, final_k=None, search_queries=None, top_k_boost=0,
                 filters=None):
        lang = language or routing.detect_language(query)
        if search_queries:
            return retrieval.corrective_retrieve(
                query, search_queries, None, specialties, lang,
                final_k=final_k, top_k_boost=top_k_boost, filters=filters,
            )
        qinfo = rewrite_queries(query, lang)
        return retrieval.corrective_retrieve(
            query, qinfo["queries"], qinfo.get("hyde"),
            specialties, lang, final_k=final_k, top_k_boost=top_k_boost,
            filters=filters,
        )

    def _merge_subquery_passages(self, query, specs, lang, filters=None) -> list[dict]:
        sub_qs = decompose_if_complex(query)
        all_passages: list[dict] = []
        seen_keys: set[str] = set()
        for sq in sub_qs:
            for p in self.retrieve(sq, specs, lang, filters=filters):
                key = f"{p.get('book_id')}:{p.get('page')}:{hash(p.get('text','')[:60])}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_passages.append(p)
        all_passages.sort(key=lambda x: x.get("score", 0), reverse=True)
        return all_passages[:RETRIEVAL.get("top_k_final", 8)]

    def _prepare_passages(self, passages: list[dict]) -> list[dict]:
        for p in passages:
            p["display_text"] = gen_display_text(p, use_parent=PARENT_CHILD)
        return passages

    def _generate(self, query: str, passages: list[dict],
                  kg_facts=None, rule_alerts=None) -> tuple[str, list[dict]]:
        passages = self._prepare_passages(passages)
        gen_ctx = passages[:RETRIEVAL.get("gen_context", 4)]
        gen_ctx = compress_passages(gen_ctx, query)
        context_block = prompts.build_context(
            gen_ctx, kg_facts=kg_facts, rule_alerts=rule_alerts,
        )
        msg = [
            {"role": "system", "content": prompts.SYSTEM_PROMPT},
            {"role": "user", "content": prompts.build_user_message(query, context_block)},
        ]
        if UNLOAD_LOCAL_BEFORE_GENERATE:
            try:
                from medrag.index import embedder
                embedder.release_local_models()
            except Exception:
                pass
        try:
            resp = routing.llm_chat(msg)
        except routing.LLMUnavailableError as e:
            return (
                f"[LLM unavailable] {e}",
                gen_ctx,
            )
        return routing.strip_thinking(resp), gen_ctx

    def answer(self, query, image_path=None, specialty=None, language=None):
        img_desc = ""
        if image_path:
            img_desc = multimodal.describe_clinical_image(image_path)
            query = f"{query}\n\n[Image analysis]: {img_desc}"

        lang = language or routing.detect_language(query)
        specs = [specialty] if specialty and specialty != "all" else routing.route_specialties(query)

        intent_info = routing.classify_intent(query) if INTENT_ROUTING else {
            "intent": "general", "filters": {},
        }
        filters = intent_info.get("filters") or {}
        rule_alerts = self._rule_alerts(query)
        kg_facts = self._kg_facts(query)

        def retrieve_fn(q, specialties, language, search_queries=None, top_k_boost=0):
            if search_queries:
                hits = self.retrieve(
                    q, specialties, language,
                    search_queries=search_queries, top_k_boost=top_k_boost,
                    filters=filters,
                )
            else:
                hits = self._merge_subquery_passages(q, specialties, language, filters)
            # Secondary broaden using CE logits when available (ColBERT fused scores are ≈[0,1])
            from medrag.rag.retrieval import _needs_broaden
            if _needs_broaden(hits):
                broad = self.retrieve(
                    q, None, None, top_k_boost=top_k_boost, filters=None,
                )
                if not hits:
                    hits = broad
                elif broad and broad[0].get("score", 0) > hits[0].get("score", -999):
                    hits = broad
            return hits

        def generate_fn(q, passages):
            return self._generate(q, passages, kg_facts=kg_facts, rule_alerts=rule_alerts)

        if SELF_RAG_ENABLED:
            result = run_loop(query, lang, specs, retrieve_fn, generate_fn)
            gen_ctx = result["passages"]
            grounding = result["grounding"]
            resp = result["answer"]
            self_rag = result.get("self_rag", {})
        else:
            passages = retrieve_fn(query, specs, lang)
            if not passages:
                return {
                    "answer": insufficient_context_answer(lang),
                    "sources": [], "specialties": specs, "image_desc": img_desc,
                    "grounding": {"grounded": 0, "confidence": "low"},
                    "self_rag": {"iterations": [], "final_action": "abstain"},
                    "intent": intent_info.get("intent"),
                    "rule_alerts": rule_alerts,
                    "kg_facts": kg_facts,
                }
            resp, gen_ctx = generate_fn(query, passages)
            from medrag.rag.grounding import verify_answer
            grounding = verify_answer(query, resp, gen_ctx)
            self_rag = {"iterations": [{"iter": 0}], "final_action": "accept"}

        sources = [{"n": i + 1, "title": p.get("title", p.get("book", "?")),
                    "page": p.get("page", p.get("page_start")),
                    "specialty": p.get("specialty"), "score": round(p.get("score", 0), 3),
                    "source_corpus": p.get("source_corpus"),
                    "chunk_id": p.get("chunk_id"), "cko_id": p.get("cko_id")}
                   for i, p in enumerate(gen_ctx)]
        return {
            "answer": resp, "sources": sources, "specialties": specs,
            "image_desc": img_desc, "grounding": grounding, "self_rag": self_rag,
            "intent": intent_info.get("intent"),
            "rule_alerts": rule_alerts,
            "kg_facts": [{"fact": f.get("fact"), "confidence": f.get("confidence")}
                         for f in kg_facts],
        }

    def answer_question_image(self, image_path: str):
        text = multimodal.ocr_image(image_path)
        questions = multimodal.split_questions(text)
        return {
            "ocr_text": text,
            "results": [{"question": q, **self.answer(q)} for q in questions],
        }

    def ocr_text(self, image_path: str) -> str:
        return multimodal.ocr_image(image_path)

    def describe_image(self, image_path: str) -> str:
        return multimodal.describe_clinical_image(image_path)
