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

from providers.base import ChatMessage, ToolCall

log = logging.getLogger("memory")


def _msg_to_dict(m: ChatMessage) -> dict:
    """Serialise a message for durable storage.

    Binary attachments (``images``/``audio``) are intentionally dropped —
    they're per-turn upload bytes already summarised into the persisted text
    via the agent's attachment note, so keeping them would just bloat the
    row without adding anything a reload needs.
    """
    return {
        "role": m.role,
        "content": m.content,
        "tool_call_id": m.tool_call_id,
        "name": m.name,
        "tool_calls": [{"name": tc.name, "arguments": tc.arguments, "id": tc.id}
                       for tc in (m.tool_calls or [])],
    }


def _msg_from_dict(d: dict) -> ChatMessage:
    return ChatMessage(
        role=d.get("role", "user"),
        content=d.get("content", ""),
        tool_call_id=d.get("tool_call_id"),
        name=d.get("name"),
        tool_calls=[ToolCall(name=tc["name"], arguments=tc.get("arguments") or {}, id=tc.get("id"))
                   for tc in (d.get("tool_calls") or [])],
    )


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
    """In-process conversation cache, durably backed by SQLite (``agent_sessions``).

    The in-process dict stays the hot path for every read/append within a
    process's lifetime — persistence only round-trips to SQLite on
    :meth:`persist` (called once per agent turn) and on a cache miss in
    :meth:`get` (rehydrating a session that was active in a previous process).
    This means a backend restart, or a request landing on a different
    horizontally-scaled worker, still finds the conversation instead of
    silently starting over — the gap the module previously documented as
    unsolved. Set ``persist=False`` to opt back into pure in-memory
    (e.g. for tests that don't want SQLite side effects).

    ``max_sessions``/``idle_ttl_sec`` bound in-process memory growth — every
    distinct session would otherwise stay resident forever. Both are read
    from ``registry.runtime`` (models.yaml) when a registry is supplied, so
    they are deployment-configurable like every other runtime knob, not
    hardcoded Python defaults.
    """

    def __init__(self, *, max_sessions: int = 2000, idle_ttl_sec: float = 6 * 3600,
                persist: bool = True) -> None:
        self._sessions: dict[str, Conversation] = {}
        self.max_sessions = max_sessions
        self.idle_ttl_sec = idle_ttl_sec
        self.persist_enabled = persist

    def get(self, session_id: str) -> Conversation:
        if session_id not in self._sessions:
            self._evict_if_needed()
            conv = self._load_from_db(session_id) if self.persist_enabled else None
            self._sessions[session_id] = conv or Conversation(session_id=session_id)
        return self._sessions[session_id]

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        if self.persist_enabled:
            try:
                import db
                with db.connect() as c:
                    c.execute("DELETE FROM agent_sessions WHERE session_id=?", (session_id,))
            except Exception as e:  # noqa: BLE001
                log.warning("memory: failed to delete persisted session %s: %s", session_id, e)

    def persist(self, session_id: str) -> None:
        """Flush one session's current state to SQLite. Call once per turn
        (after the agent loop finishes appending) — not on every append, to
        avoid a DB round-trip per message.

        Synchronous: SQLite writes are fast and this keeps a simple call site
        for non-async callers. From async code prefer :meth:`persist_async`,
        which runs this off the event loop thread so one session's flush
        can't stall other requests being served by the same worker.
        """
        if not self.persist_enabled or session_id not in self._sessions:
            return
        conv = self._sessions[session_id]
        try:
            import db
            payload = db.dumps_json([_msg_to_dict(m) for m in conv.messages])
            with db.connect() as c:
                c.execute(
                    """INSERT INTO agent_sessions (session_id, messages, summary, last_used)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(session_id) DO UPDATE SET
                         messages=excluded.messages,
                         summary=excluded.summary,
                         last_used=excluded.last_used""",
                    (session_id, payload, conv.summary, conv.last_used),
                )
        except Exception as e:  # noqa: BLE001
            log.warning("memory: failed to persist session %s: %s", session_id, e)

    async def persist_async(self, session_id: str) -> None:
        """Async wrapper around :meth:`persist` — offloads the SQLite write to
        a worker thread (``asyncio.to_thread``) so it doesn't block the event
        loop. SQLite still serializes writers under the hood, so this doesn't
        raise write throughput by itself, but it stops one session's flush
        from adding event-loop latency to every other concurrent request on
        the same worker — the cheap first step before reaching for a write
        queue, which is only worth the complexity if load actually needs it.
        """
        await asyncio.to_thread(self.persist, session_id)

    def purge_stale(self, idle_ttl_sec: float | None = None) -> int:
        """Delete durable rows older than ``idle_ttl_sec`` (default: this
        store's own idle_ttl_sec). Idle-TTL eviction in :meth:`_evict_if_needed`
        only bounds the in-process cache -- the SQLite table accumulates
        every session ever seen unless something purges it too. Returns the
        number of rows deleted. Safe to call from any process; a scheduled
        caller doesn't need to be the same worker that wrote the rows.
        """
        if not self.persist_enabled:
            return 0
        ttl = self.idle_ttl_sec if idle_ttl_sec is None else idle_ttl_sec
        cutoff = time.time() - ttl
        try:
            import db
            with db.connect() as c:
                cur = c.execute("DELETE FROM agent_sessions WHERE last_used < ?", (cutoff,))
                deleted = cur.rowcount
            if deleted:
                log.info("memory: purged %d stale session(s) older than %.0fs", deleted, ttl)
            return deleted
        except Exception as e:  # noqa: BLE001
            log.warning("memory: purge_stale failed: %s", e)
            return 0

    @staticmethod
    def _load_from_db(session_id: str) -> "Conversation | None":
        try:
            import db
            with db.connect() as c:
                row = c.execute(
                    "SELECT messages, summary, last_used FROM agent_sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
            if row is None:
                return None
            messages = [_msg_from_dict(d) for d in (db.loads_json(row["messages"]) or [])]
            log.info("memory: rehydrated session %s from disk (%d msgs)", session_id, len(messages))
            return Conversation(session_id=session_id, messages=messages,
                                summary=row["summary"] or "", last_used=row["last_used"])
        except Exception as e:  # noqa: BLE001
            log.warning("memory: failed to load persisted session %s: %s", session_id, e)
            return None

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
