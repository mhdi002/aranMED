"""Medical-education tutor tools for students & residents.

A lightweight teaching assistant that the core LLM can drive to generate:

* ``make_mcq``       — board-style multiple-choice questions with answers
                       and explanations,
* ``make_case_study``— structured clinical vignettes with guided questions,
* ``make_mock_exam`` — a mixed mini-exam (several MCQs + a short case),
* ``explain_concept``— a focused teaching explanation of a topic.

All tools support English and Persian output and return both human-readable
text *and* a structured ``data`` payload so the frontend can render
interactive quizzes.
"""
from __future__ import annotations

import json
import logging
import re

from .base import Tool, ToolContext, ToolResult, tool

log = logging.getLogger("tools.education")


def _lang_clause(language: str) -> str:
    return ("Write everything in Persian (فارسی)."
            if language == "fa" else "Write everything in English.")


def _extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i != -1],
                default=-1)
    if start == -1:
        raise ValueError("no JSON found")
    # find matching closing bracket of the same type
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    end = text.rfind(close_ch)
    return json.loads(text[start:end + 1])


@tool
class MakeMCQTool(Tool):
    name = "make_mcq"
    description = (
        "Generate board-style multiple-choice questions on a medical topic for "
        "students or residents. Returns questions with options, the correct "
        "answer, and an explanation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "count": {"type": "integer", "minimum": 1, "maximum": 20},
            "difficulty": {"type": "string",
                           "enum": ["student", "resident", "board"]},
            "language": {"type": "string", "enum": ["en", "fa"]},
        },
        "required": ["topic"],
    }

    async def run(self, ctx: ToolContext, topic: str, count: int = 5,
                  difficulty: str = "resident",
                  language: str = "en") -> ToolResult:
        from providers.base import ChatMessage
        count = max(1, min(int(count), 20))
        sys = (
            "You are a medical educator writing high-quality, single-best-answer "
            f"multiple-choice questions at the '{difficulty}' level. "
            f"{_lang_clause(language)} "
            "Return ONLY JSON (no markdown) with this schema:\n"
            '{"questions":[{"stem":str,"options":["A ...","B ...","C ...","D ...",'
            '"E ..."],"answer_index":int,"explanation":str}]}'
        )
        usr = f"Topic: {topic}\nNumber of questions: {count}"
        core = await ctx.registry.get_text("core")
        out = await core.chat(
            [ChatMessage(role="system", content=sys),
             ChatMessage(role="user", content=usr)],
            temperature=0.4, max_tokens=2000,
        )
        try:
            data = _extract_json(out.content)
            questions = data.get("questions", data) if isinstance(data, dict) else data
        except Exception as e:  # noqa: BLE001
            return ToolResult(content=out.content,
                              error=f"could not parse MCQ JSON: {e}",
                              data={"raw": out.content})
        return ToolResult(
            content=f"Generated {len(questions)} MCQ(s) on '{topic}'.",
            data={"topic": topic, "difficulty": difficulty,
                  "language": language, "questions": questions},
        )


@tool
class MakeCaseStudyTool(Tool):
    name = "make_case_study"
    description = (
        "Create a structured clinical case study / vignette with guided "
        "teaching questions for students or residents."
    )
    parameters = {
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "specialty": {"type": "string"},
            "difficulty": {"type": "string",
                           "enum": ["student", "resident", "board"]},
            "language": {"type": "string", "enum": ["en", "fa"]},
        },
        "required": ["topic"],
    }

    async def run(self, ctx: ToolContext, topic: str,
                  specialty: str | None = None,
                  difficulty: str = "resident",
                  language: str = "en") -> ToolResult:
        from providers.base import ChatMessage
        sys = (
            "You are a clinical educator. Build one realistic teaching case. "
            f"{_lang_clause(language)} "
            "Return ONLY JSON with schema:\n"
            '{"title":str,"presentation":str,"history":str,"exam":str,'
            '"investigations":str,"questions":[{"q":str,"answer":str}],'
            '"teaching_points":[str]}'
        )
        usr = f"Topic: {topic}\nSpecialty: {specialty or 'general'}\nLevel: {difficulty}"
        core = await ctx.registry.get_text("core")
        out = await core.chat(
            [ChatMessage(role="system", content=sys),
             ChatMessage(role="user", content=usr)],
            temperature=0.5, max_tokens=2000,
        )
        try:
            data = _extract_json(out.content)
        except Exception as e:  # noqa: BLE001
            return ToolResult(content=out.content,
                              error=f"could not parse case JSON: {e}",
                              data={"raw": out.content})
        return ToolResult(
            content=f"Case study '{data.get('title', topic)}' ready.",
            data={"topic": topic, "language": language, "case": data},
        )


@tool
class MakeMockExamTool(Tool):
    name = "make_mock_exam"
    description = (
        "Assemble a short mock exam combining several MCQs and one case study "
        "for self-assessment."
    )
    parameters = {
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "mcq_count": {"type": "integer", "minimum": 1, "maximum": 15},
            "difficulty": {"type": "string",
                           "enum": ["student", "resident", "board"]},
            "language": {"type": "string", "enum": ["en", "fa"]},
        },
        "required": ["topic"],
    }

    async def run(self, ctx: ToolContext, topic: str, mcq_count: int = 5,
                  difficulty: str = "resident",
                  language: str = "en") -> ToolResult:
        mcq = await MakeMCQTool().run(ctx, topic=topic, count=mcq_count,
                                      difficulty=difficulty, language=language)
        case = await MakeCaseStudyTool().run(ctx, topic=topic,
                                             difficulty=difficulty,
                                             language=language)
        questions = (mcq.data or {}).get("questions", [])
        return ToolResult(
            content=(f"Mock exam on '{topic}': {len(questions)} MCQs + 1 case."),
            data={"topic": topic, "language": language,
                  "mcqs": questions,
                  "case": (case.data or {}).get("case")},
        )


@tool
class ExplainConceptTool(Tool):
    name = "explain_concept"
    description = (
        "Give a focused, level-appropriate teaching explanation of a medical "
        "concept for a student or resident."
    )
    parameters = {
        "type": "object",
        "properties": {
            "concept": {"type": "string"},
            "difficulty": {"type": "string",
                           "enum": ["student", "resident", "board"]},
            "language": {"type": "string", "enum": ["en", "fa"]},
        },
        "required": ["concept"],
    }

    async def run(self, ctx: ToolContext, concept: str,
                  difficulty: str = "student",
                  language: str = "en") -> ToolResult:
        from providers.base import ChatMessage
        sys = (
            "You are a patient, accurate medical tutor. Explain the requested "
            f"concept at the '{difficulty}' level with a clear structure "
            "(definition, mechanism, clinical relevance, key pearls). "
            f"{_lang_clause(language)}"
        )
        core = await ctx.registry.get_text("core")
        out = await core.chat(
            [ChatMessage(role="system", content=sys),
             ChatMessage(role="user", content=concept)],
            temperature=0.4, max_tokens=1200,
        )
        return ToolResult(
            content=out.content,
            data={"concept": concept, "language": language,
                  "explanation": out.content},
        )
