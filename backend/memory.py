"""Conversation memory with sliding-window + auto-summary.

We keep the last ``keep_turns`` user/assistant pairs verbatim. When the
total exceeds ``summary_trigger`` the oldest turns are condensed by the core
LLM into a single ``system`` summary message. This keeps prompt size bounded
without losing long-range context.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from providers.base import ChatMessage

log = logging.getLogger("memory")


@dataclass
class Conversation:
    session_id: str
    messages: list[ChatMessage] = field(default_factory=list)
    summary: str = ""
    last_used: float = field(default_factory=time.time)

    def append(self, m: ChatMessage) -> None:
        self.messages.append(m)
        self.last_used = time.time()


class MemoryStore:
    """In-process conversation cache. Swap with Redis/SQLite for multi-process."""

    def __init__(self) -> None:
        self._sessions: dict[str, Conversation] = {}

    def get(self, session_id: str) -> Conversation:
        if session_id not in self._sessions:
            self._sessions[session_id] = Conversation(session_id=session_id)
        return self._sessions[session_id]

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def render(
        self,
        session_id: str,
        *,
        system_prompt: str,
        registry: Any,
        keep_turns: int = 12,
        summary_trigger: int = 24,
    ) -> list[ChatMessage]:
        """Return the message list to send to the LLM, summarising if needed."""
        conv = self.get(session_id)
        # Count "turns" as user messages.
        user_count = sum(1 for m in conv.messages if m.role == "user")
        if user_count > summary_trigger:
            await self._compact(conv, registry, keep_turns)

        out: list[ChatMessage] = [ChatMessage(role="system", content=system_prompt)]
        if conv.summary:
            out.append(ChatMessage(role="system",
                                   content=f"Earlier conversation summary:\n{conv.summary}"))
        out.extend(conv.messages)
        return out

    async def _compact(self, conv: Conversation, registry: Any, keep_turns: int) -> None:
        """Summarise everything except the most recent ``keep_turns`` user turns."""
        # Find cut index: last `keep_turns` user messages onward stay verbatim.
        u_idx = [i for i, m in enumerate(conv.messages) if m.role == "user"]
        if len(u_idx) <= keep_turns:
            return
        cut = u_idx[-keep_turns]
        old, new = conv.messages[:cut], conv.messages[cut:]
        if not old:
            return

        rendered = "\n".join(f"{m.role}: {m.content[:600]}" for m in old if m.content)
        prompt = (
            "Summarise the following conversation between a radiologist and an "
            "AI assistant. Focus on: prior findings, agreed templates, patient "
            "context, decisions taken, pending tasks. Keep it ≤ 250 words.\n\n"
            f"{rendered}"
        )
        try:
            core = await registry.get_text("core")
            res = await core.chat(
                [ChatMessage(role="system", content="You produce concise factual summaries."),
                 ChatMessage(role="user", content=prompt)],
                temperature=0.2, max_tokens=400,
            )
            conv.summary = ((conv.summary + "\n" if conv.summary else "") + res.content).strip()
            conv.messages = new
            log.info("memory: compacted %d → %d msgs (summary=%d chars)",
                     len(old) + len(new), len(new), len(conv.summary))
        except Exception as e:  # noqa: BLE001
            log.warning("memory compact failed: %s", e)
