"""System-prompt loader.

Prompt text lives in ``backend/data/prompts/*.txt`` (override the directory
with ``PROMPTS_DIR``) rather than as Python string literals, matching the
pattern ``templates.py`` and ``report_rules.py`` already use for their
clinical content: text a clinician may need to review or tune should be
editable without a code change or a redeploy of application logic.

Each module keeps its literal as the in-code fallback, so a missing or
unreadable file degrades to the previously-shipped prompt rather than
breaking the agent. Loads are cached; call :func:`reload` after editing a
file in a long-running process.
"""
from __future__ import annotations

import logging
import threading

import config

log = logging.getLogger("prompts")

_cache: dict[str, str] = {}
_lock = threading.Lock()


def get(name: str, default: str = "") -> str:
    """Return prompt ``name`` from ``PROMPTS_DIR/<name>.txt``.

    Falls back to *default* (the caller's in-code literal) when the file is
    absent or unreadable, so an incomplete deployment can never leave the
    agent with an empty system prompt.
    """
    with _lock:
        if name in _cache:
            return _cache[name]
    path = config.PROMPTS_DIR / f"{name}.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError("empty prompt file")
    except (OSError, ValueError) as e:
        log.warning("prompts: falling back to in-code default for %r (%s)", name, e)
        text = default
    with _lock:
        _cache[name] = text
    return text


def reload() -> None:
    """Drop the cache so the next :func:`get` re-reads from disk."""
    with _lock:
        _cache.clear()
