"""Base classes for the agent's tool system.

A *Tool* is a thin object exposing:

* ``name`` / ``description`` / ``parameters`` (JSON-Schema) — what the LLM sees
* ``async run(ctx, **arguments) -> ToolResult`` — what we actually execute

Tools register themselves at import time via :func:`registry.register`.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("tools")


@dataclass
class ToolContext:
    """Per-call execution context, passed to every tool.

    The context is built fresh for each user turn and carries:

    * ``registry`` — the model registry, so a tool can ask for any role
      (e.g. ``await ctx.registry.get_asr()``).
    * ``state`` — a mutable dict shared between tools in the same turn
      (so :func:`transcribe_audio` can leave the transcript for
      :func:`structure_report` to pick up automatically).
    * ``attachments`` — bytes uploaded by the user with the current request,
      keyed by an opaque id (``audio:0``, ``image:0`` …).
    """
    registry: Any
    state: dict = field(default_factory=dict)
    attachments: dict[str, bytes] = field(default_factory=dict)
    templates: Any = None  # templates module (lazy)


@dataclass
class ToolResult:
    """Return value from a tool invocation."""
    content: str                       # text the LLM will see next turn
    data: dict | None = None           # structured payload returned to the API caller
    error: str | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"content": self.content}
        if self.data is not None:
            d["data"] = self.data
        if self.error:
            d["error"] = self.error
        return d


class Tool(abc.ABC):
    name: str = ""
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}}

    @abc.abstractmethod
    async def run(self, ctx: ToolContext, **arguments: Any) -> ToolResult: ...

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


# --- registry ---------------------------------------------------------
class _Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if not tool.name:
            raise ValueError("tool.name required")
        if tool.name in self._tools:
            log.warning("tool %s replaced", tool.name)
        self._tools[tool.name] = tool
        return tool

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        return self._tools[name]


registry = _Registry()


def tool(cls: type[Tool]) -> type[Tool]:
    """Class decorator: instantiate + register."""
    inst = cls()
    registry.register(inst)
    return cls
