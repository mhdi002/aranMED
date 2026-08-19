"""Conversation memory with sliding-window + auto-summary.

We keep the last ``keep_turns`` user/assistant pairs verbatim. When the
total exceeds ``summary_trigger`` the oldest turns are condensed by the core
LLM into a single ``system`` summary message. This keeps prompt size bounded
without losing long-range context.
"""
from __future__ import annotations

import asyncio
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
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    def append(self, m: ChatMessage) -> None:
        self.messages.append(m)
        self.last_used = time.time()


class MemoryStore:
    """In-process conversation cache. Swap with Redis/SQLite for multi-process.

    ``max_sessions``/``idle_ttl_sec`` bound memory growth — every distinct
    session would otherwise stay resident forever. Both are read from
    ``registry.runtime`` (models.yaml) when a registry is supplied, so they
    are deployment-configurable like every other runtime knob, not
    hardcoded Python defaults.
    """

    def __init__(self, *, max_sessions: int = 2000, idle_ttl_sec: float = 6 * 3600) -> None:
        self._sessions: dict[str, Conversation] = {}
        self.max_sessions = max_sessions
        self.idle_ttl_sec = idle_ttl_sec

    def get(self, session_id: str) -> Conversation:
        if session_id not in self._sessions:
            self._evict_if_needed()
            self._sessions[session_id] = Conversation(session_id=session_id)
        return self._sessions[session_id]

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def _evict_if_needed(self) -> None:
        now = time.time()
        stale = [sid for sid, c in self._sessions.items()
                 if now - c.last_used > self.idle_ttl_sec]
        for sid in stale:
            self._sessions.pop(sid, None)
        if len(self._sessions) >= self.max_sessions:
            oldest = sorted(self._sessions.values(), key=lambda c: c.last_used)
            for c in oldest[: max(1, len(self._sessions) - self.max_sessions + 1)]:
                self._sessions.pop(c.session_id, None)
            log.warning("memory: evicted sessions to stay under max_sessions=%d",
                       self.max_sessions)

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
        async with conv.lock:
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
        """Summarise everything except the most recent ``keep_turns`` user turns.

        Caller must hold ``conv.lock`` — this mutates ``conv.messages`` across
        an awaited LLM call, which would otherwise race with a concurrent
        ``append()`` for the same session.
        """
        runtime = getattr(registry, "runtime", {}) or {}
        char_limit = int(runtime.get("memory_summary_char_limit", 600))
        summary_max_tokens = int(runtime.get("memory_summary_max_tokens", 400))
        summary_word_limit = int(runtime.get("memory_summary_word_limit", 250))

        # Find cut index: last `keep_turns` user messages onward stay verbatim.
        u_idx = [i for i, m in enumerate(conv.messages) if m.role == "user"]
        if len(u_idx) <= keep_turns:
            return
        cut = u_idx[-keep_turns]
        old, new = conv.messages[:cut], conv.messages[cut:]
        if not old:
            return

        rendered = "\n".join(f"{m.role}: {m.content[:char_limit]}" for m in old if m.content)
        prompt = (
            "Summarise the following conversation between a radiologist and an "
            "AI assistant. Focus on: prior findings, agreed templates, patient "
            f"context, decisions taken, pending tasks. Keep it ≤ {summary_word_limit} words.\n\n"
            f"{rendered}"
        )
        try:
            core = await registry.get_text("core")
            res = await core.chat(
                [ChatMessage(role="system", content="You produce concise factual summaries."),
                 ChatMessage(role="user", content=prompt)],
                temperature=0.2, max_tokens=summary_max_tokens,
            )
            conv.summary = ((conv.summary + "\n" if conv.summary else "") + res.content).strip()
            # Messages appended by a racing caller after `old`/`new` were cut
            # (impossible while conv.lock is held, but keep this correct even
            # if a future caller compacts without the lock) are preserved by
            # replacing only the prefix, never truncating past `new`'s tail.
            conv.messages = new + conv.messages[len(old) + len(new):]
            log.info("memory: compacted %d → %d msgs (summary=%d chars)",
                     len(old) + len(new), len(conv.messages), len(conv.summary))
        except Exception as e:  # noqa: BLE001
            log.warning("memory compact failed: %s", e)
