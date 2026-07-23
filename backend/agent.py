"""Agent orchestrator.

Implements a tool-calling loop on top of any :class:`TextProvider`:

  user ──▶ core LLM ──▶ (tool call?) ──▶ tool ──▶ result ──▶ core LLM ──▶ …

For providers that natively support tool calls (Ollama, OpenAI-compat,
llama.cpp server) we use the structured ``tool_calls`` field. For providers
without native tool support (HF transformers, llama-cpp-python without
grammars) the providers themselves fall back to a JSON-prompted convention,
which is parsed transparently by their ``.chat()`` implementations.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import templates as templates_mod
from memory import MemoryStore
from providers.base import ChatMessage, ToolCall
from registry import Registry
from tools import ToolContext, registry as tool_registry

log = logging.getLogger("agent")


SYSTEM_PROMPT = """You are aranmed, an orchestration assistant for a bilingual
(Persian + English) radiology workflow.

Style:
  • Be concise, professional, and clinical.
  • Translate Persian phrases to precise clinical English.
  • Never invent findings — when in doubt, recommend clinical correlation.

Tools:
  • You can call tools to (a) transcribe audio, (b) describe medical images,
    (c) list/load report templates, and (d) re-structure free-form text into
    a chosen template.
  • Prefer calling tools instead of guessing — e.g. if the user uploads audio
    AND asks for a thyroid sonography report, call `transcribe_audio` then `structure_report`
    with template_id="thyroid".
  • If the user already provides a transcript / draft and only wants the
    template applied, skip the ASR step and call `structure_report` directly.

When you finish, return the final answer in plain text. If a tool produced
a structured report, include the report verbatim.
"""


@dataclass
class AgentResult:
    answer: str
    tool_calls: list[dict] = field(default_factory=list)
    state: dict = field(default_factory=dict)
    model: str = ""


class Agent:
    def __init__(self, *, registry: Registry, memory: MemoryStore) -> None:
        self.registry = registry
        self.memory = memory
        self.runtime = registry.runtime

    async def run(
        self,
        *,
        session_id: str,
        user_text: str,
        attachments: Optional[dict[str, bytes]] = None,
        max_iters: int = 5,
        core_name: Optional[str] = None,
    ) -> AgentResult:
        attachments = attachments or {}
        ctx = ToolContext(registry=self.registry, attachments=attachments,
                          templates=templates_mod)

        # 1) extend memory with the new user turn (annotated with what's attached).
        attach_note = ""
        if attachments:
            attach_note = ("\n[attachments: " +
                           ", ".join(f"{k}({len(v)} bytes)" for k, v in attachments.items())
                           + "]")
        conv = self.memory.get(session_id)
        conv.append(ChatMessage(role="user", content=user_text + attach_note))

        # 2) prep messages with sliding-window/summary
        sys_with_ids = SYSTEM_PROMPT
        if attachments:
            sys_with_ids += (
                "\n\nThis turn has the following attachments — pass these EXACT "
                "ids as `attachment_id` to the relevant tool:\n  - "
                + "\n  - ".join(attachments.keys())
            )
        msgs = await self.memory.render(
            session_id,
            system_prompt=sys_with_ids,
            registry=self.registry,
            keep_turns=int(self.runtime.get("history_keep_turns", 12)),
            summary_trigger=int(self.runtime.get("history_summary_trigger", 24)),
        )

        core = await self.registry.get_text("core", name=core_name)
        tool_schemas = tool_registry.schemas()
        all_calls: list[dict] = []

        for step in range(max_iters):
            out = await core.chat(
                msgs,
                tools=tool_schemas if core.supports_tools else None,
                temperature=0.2,
                max_tokens=1400,
            )
            msgs.append(out)
            conv.append(out)

            if not out.tool_calls:
                return AgentResult(answer=out.content, tool_calls=all_calls,
                                   state=ctx.state, model=core.name)

            for tc in out.tool_calls:
                result = await self._invoke(tc, ctx)
                all_calls.append({"name": tc.name, "arguments": tc.arguments,
                                  "result": result.to_dict()})
                tool_msg = ChatMessage(
                    role="tool",
                    name=tc.name,
                    tool_call_id=tc.id or "",
                    content=(result.error and f"ERROR: {result.error}") or result.content,
                )
                msgs.append(tool_msg)
                conv.append(tool_msg)

        # Hit max iters without a final answer; ask one more time, no tools.
        final = await core.chat(msgs, tools=None, temperature=0.2, max_tokens=900)
        conv.append(final)
        return AgentResult(answer=final.content, tool_calls=all_calls,
                           state=ctx.state, model=core.name)

    async def _invoke(self, tc: ToolCall, ctx: ToolContext):
        log.info("→ tool %s %s", tc.name, tc.arguments)
        try:
            tool = tool_registry.get(tc.name)
        except KeyError:
            # Try fuzzy match — LLMs occasionally invent close-but-wrong names
            # like "list_report_templates" instead of "list_templates".
            fuzzy = self._fuzzy_lookup(tc.name)
            if fuzzy is None:
                from tools import ToolResult
                return ToolResult(
                    content="",
                    error=(f"unknown tool '{tc.name}'. Valid tools: "
                           + ", ".join(t.name for t in tool_registry.list())),
                )
            tool = fuzzy
        args = self._normalise_args(tool, tc.arguments)
        try:
            return await tool.run(ctx, **args)
        except TypeError as e:
            from tools import ToolResult
            return ToolResult(content="", error=f"bad arguments: {e}")
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed", tc.name)
            from tools import ToolResult
            return ToolResult(content="", error=str(e))

    @staticmethod
    def _fuzzy_lookup(name: str):
        from tools import registry as tr
        names = [t.name for t in tr.list()]
        # exact-substring fallback
        cand = [n for n in names if n in name or name in n]
        if len(cand) == 1:
            return tr.get(cand[0])
        # token-overlap fallback (e.g. "list_report_templates" → "list_templates")
        want = set(name.lower().replace("-", "_").split("_"))
        scored: list[tuple[int, str]] = []
        for n in names:
            toks = set(n.lower().split("_"))
            shared = len(want & toks)
            if shared >= 2:
                scored.append((shared, n))
        if scored:
            scored.sort(reverse=True)
            return tr.get(scored[0][1])
        return None

    @staticmethod
    def _normalise_args(tool, arguments) -> dict:
        """Map common LLM argument-name variants onto the tool's parameter names."""
        if not isinstance(arguments, dict):
            return {}
        # Aliases the model often invents.
        ALIASES = {
            "audio_id": "attachment_id",
            "audio_file": "attachment_id",
            "audio": "attachment_id",
            "audio_path": "attachment_id",
            "image_id": "attachment_id",
            "image_file": "attachment_id",
            "image": "attachment_id",
            "image_path": "attachment_id",
            "file": "attachment_id",
            "file_id": "attachment_id",
            "id": "attachment_id",
            "lang": "language",
            "transcript_text": "transcript",
            "text": "transcript",
            "template": "template_id",
            "template_name": "template_id",
        }
        valid = set((tool.parameters or {}).get("properties", {}).keys())
        out: dict = {}
        for k, v in arguments.items():
            if k in valid:
                out[k] = v
            elif k in ALIASES and ALIASES[k] in valid and ALIASES[k] not in out:
                out[ALIASES[k]] = v
        return out
