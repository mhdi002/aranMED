"""Agent tools for bilingual radiology ASR → English dictation."""
from __future__ import annotations

from .base import Tool, ToolContext, ToolResult, tool

_REPAIR_SYS = """You convert raw ASR from bilingual Persian–English radiology dictation into clear clinical English.

Rules:
1. Translate ALL Persian into accurate clinical English.
2. Fix ASR misspellings of English medical terms.
3. Preserve every finding, organ, and measurement — do NOT drop content.
4. Do NOT invent findings.
5. Output one continuous English dictation paragraph only.
"""


@tool
class RepairCodeSwitchTranscriptTool(Tool):
    name = "repair_codeswitch_transcript"
    description = (
        "Convert a raw bilingual Persian–English ASR transcript into clean English "
        "clinical dictation. Use after transcribe_audio."
    )
    parameters = {
        "type": "object",
        "properties": {
            "transcript": {
                "type": "string",
                "description": "Raw ASR text. If omitted, uses the latest session transcript.",
            },
        },
        "required": [],
    }

    async def run(self, ctx: ToolContext, transcript: str | None = None) -> ToolResult:
        from english_transcript import to_english_clinical

        raw = (transcript or ctx.state.get("transcript") or "").strip()
        if not raw:
            return ToolResult(content="", error="no transcript to repair")

        english = await to_english_clinical(raw, registry=ctx.registry)
        ctx.state["transcript"] = english
        ctx.state["raw_transcript"] = raw
        ctx.state["transcript_repaired"] = True
        return ToolResult(
            content=f"English dictation:\n{english}",
            data={"transcript": english, "original": raw},
        )
