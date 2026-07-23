"""Built-in tools available to the core LLM."""
from __future__ import annotations

import logging
from typing import Any

from .base import Tool, ToolContext, ToolResult, tool

log = logging.getLogger("tools.builtin")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
@tool
class ListTemplatesTool(Tool):
    name = "list_templates"
    description = ("List the available radiology report templates "
                   "(modality + body-part scaffolds) the agent can fill in.")
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, ctx: ToolContext) -> ToolResult:
        items = ctx.templates.list_templates()
        names = [t["id"] for t in items]
        return ToolResult(
            content="Available templates: " + ", ".join(names),
            data={"templates": items},
        )


@tool
class GetTemplateTool(Tool):
    name = "get_template"
    description = "Return the body of a specific report template, useful as a scaffold."
    parameters = {
        "type": "object",
        "properties": {
            "template_id": {"type": "string", "description": "The template id, e.g. ct_chest"},
        },
        "required": ["template_id"],
    }

    async def run(self, ctx: ToolContext, template_id: str) -> ToolResult:
        try:
            body = ctx.templates.get_template(template_id)
        except FileNotFoundError as e:
            return ToolResult(content="", error=f"unknown template: {template_id}")
        ctx.state["template_id"] = template_id
        ctx.state["template_body"] = body
        return ToolResult(
            content=f"Template '{template_id}' loaded ({len(body)} chars).",
            data={"template_id": template_id, "body": body},
        )


# ---------------------------------------------------------------------------
# ASR
# ---------------------------------------------------------------------------
@tool
class TranscribeAudioTool(Tool):
    name = "transcribe_audio"
    description = ("Run the ASR model on an attached audio file and return "
                   "the transcript. Use when the user uploads audio.")
    parameters = {
        "type": "object",
        "properties": {
            "attachment_id": {"type": "string",
                              "description": "Id of the audio attachment, e.g. 'audio:0'"},
            "language": {"type": "string", "description": "Optional ISO code (fa, en…)"},
        },
        "required": ["attachment_id"],
    }

    async def run(self, ctx: ToolContext, attachment_id: str,
                  language: str | None = None) -> ToolResult:
        buf = ctx.attachments.get(attachment_id)
        if not buf:
            return ToolResult(content="", error=f"no attachment with id {attachment_id}")
        asr = await ctx.registry.get_asr()
        text = await asr.transcribe(buf, language=language)
        ctx.state["transcript"] = text
        ctx.state["asr_model"] = asr.name
        return ToolResult(
            content=f"Transcript:\n{text}",
            data={"transcript": text, "asr_model": asr.name},
        )


# ---------------------------------------------------------------------------
# Vision
# ---------------------------------------------------------------------------
@tool
class DescribeImageTool(Tool):
    name = "describe_image"
    description = ("Send an attached medical image to the vision LLM and "
                   "obtain findings/observations. Use whenever the user asks "
                   "about an image (X-ray, CT slice, ultrasound still, etc.).")
    parameters = {
        "type": "object",
        "properties": {
            "attachment_id": {"type": "string",
                              "description": "Id of the image attachment, e.g. 'image:0'"},
            "question": {"type": "string",
                         "description": "What the user wants to know about the image."},
        },
        "required": ["attachment_id", "question"],
    }

    async def run(self, ctx: ToolContext, attachment_id: str,
                  question: str) -> ToolResult:
        from providers.base import ChatMessage
        buf = ctx.attachments.get(attachment_id)
        if not buf:
            return ToolResult(content="", error=f"no attachment with id {attachment_id}")
        v = await ctx.registry.get_vision()
        msg = ChatMessage(role="user", content=question, images=[buf])
        sys = ChatMessage(
            role="system",
            content=("You are a board-certified radiologist analysing the supplied "
                     "medical image. Be precise, hedge appropriately, do not invent "
                     "findings, and always recommend clinical correlation."),
        )
        out = await v.chat([sys, msg], temperature=0.2, max_tokens=600)
        ctx.state["vision_model"] = v.name
        return ToolResult(
            content=f"Vision model says:\n{out.content}",
            data={"description": out.content, "vision_model": v.name},
        )


# ---------------------------------------------------------------------------
# Report structuring
# ---------------------------------------------------------------------------
_STRUCTURE_SYS_BASE = """You are a board-certified radiologist's drafting assistant.
You receive:
  • a free-form dictation or rough draft (possibly bilingual: Persian + English),
  • a structured TEMPLATE the user manually selected (institutional wording),
  • hospital/insurance REPORT RULES about exact exam titles (never invent shortened titles).

Your job:
  1. Translate any Persian phrases to clinical English while keeping anatomical precision.
  2. Obey the SELECTED TEMPLATE's exact title, section order, and headings. Prefer that
     structure over any other exam style mentioned in the dictation.
  3. Use ONLY information present in the dictation. Where the dictation is silent on a
     section, write "Not dictated." — do NOT invent findings.
  4. Exam titles must match the official catalogue / template title EXACTLY
     (e.g. full «neck soft tissue ct» — never a shortened «neck ct»).
  5. If the spoken/dictated exam name or content style clearly mismatches the selected
     template, still fill the SELECTED template, and add a short trailing note:
     "NOTE: Dictation appears to describe <X> while template <Y> was selected."
  6. Output plain text, no markdown fences.
"""

try:
    from clinical_safety import structure_system_prompt as _structure_sys_wrap

    _STRUCTURE_SYS = _structure_sys_wrap(_STRUCTURE_SYS_BASE)
except Exception:  # noqa: BLE001
    _STRUCTURE_SYS = _STRUCTURE_SYS_BASE


@tool
class StructureReportTool(Tool):
    name = "structure_report"
    description = ("Take an existing free-form dictation OR draft and reshape it "
                   "into a structured radiology report following a chosen template. "
                   "If 'transcript' is omitted, the most recent transcript from this "
                   "session is used. If 'template_id' is omitted, the most recent "
                   "loaded template is used.")
    parameters = {
        "type": "object",
        "properties": {
            "transcript": {"type": "string",
                           "description": "Free-form dictation/draft text."},
            "template_id": {"type": "string"},
            "extra_context": {"type": "string"},
        },
        "required": [],
    }

    async def run(self, ctx: ToolContext, transcript: str | None = None,
                  template_id: str | None = None,
                  extra_context: str | None = None) -> ToolResult:
        from providers.base import ChatMessage
        import report_rules as rules_mod

        transcript = transcript or ctx.state.get("transcript")
        if not transcript:
            return ToolResult(content="", error="no transcript provided or available")
        template_id = template_id or ctx.state.get("template_id")
        template_title = ""
        if template_id and "template_body" in ctx.state and \
                ctx.state.get("template_id") == template_id:
            template_body = ctx.state["template_body"]
        elif template_id:
            template_body = ctx.templates.get_template(template_id)
            try:
                template_title = (
                    ctx.templates.get_template_meta(template_id).get("title") or ""
                )
            except Exception:  # noqa: BLE001
                template_title = rules_mod.official_title_for(template_id)
        else:
            template_body = "(no template — produce a generic radiology report layout)"

        if not template_title and template_id:
            template_title = rules_mod.official_title_for(template_id)
        if not template_title and template_body:
            for ln in template_body.splitlines():
                t = ln.strip().rstrip(":").strip()
                if t and not t.startswith("#"):
                    template_title = t
                    break

        medrag_ans = await rules_mod.consult_medrag_naming_rules(
            template_title=template_title, transcript=transcript or "",
        )
        merged_ctx = rules_mod.build_report_context(
            template_title=template_title,
            transcript=transcript or "",
            extra_context=extra_context,
            medrag_answer=medrag_ans,
        )

        prompt = (
            f"=== TEMPLATE (id={template_id or 'none'}; "
            f"title={template_title or 'unknown'}) ===\n{template_body}\n\n"
            f"=== DICTATION ===\n{transcript}\n\n"
            "NOTE: Fill the TEMPLATE above exactly. The UI-selected template wins "
            "over any exam/template name spoken in the dictation.\n\n"
        )
        if merged_ctx:
            prompt += f"=== ADDITIONAL CONTEXT ===\n{merged_ctx}\n\n"
        prompt += "=== FILLED REPORT ===\n"

        core = await ctx.registry.get_text("core")
        out = await core.chat(
            [ChatMessage(role="system", content=_STRUCTURE_SYS),
             ChatMessage(role="user", content=prompt)],
            temperature=0.2, max_tokens=1400,
        )
        ctx.state["report"] = out.content
        safety: dict = {}
        try:
            from clinical_safety import enrich_report_payload

            safety = await enrich_report_payload(
                transcript=transcript,
                report_text=out.content,
                template_id=template_id or "",
                use_medrag=False,  # keep tool path fast; HTTP endpoints may escalate
            )
            ctx.state["critical_alerts"] = safety.get("critical_alerts") or []
            ctx.state["template_mismatch"] = safety.get("template_mismatch")
        except Exception as e:  # noqa: BLE001
            log.warning("clinical safety enrich failed: %s", e)
        return ToolResult(
            content="Structured report drafted.",
            data={
                "report": out.content,
                "template_id": template_id,
                **safety,
            },
        )
