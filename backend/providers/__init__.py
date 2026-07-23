"""Provider abstractions: a unified interface over Ollama, llama.cpp,
HuggingFace transformers, OpenAI-compatible servers, etc.

Every backend (text-LLM / vision-LLM / ASR) implements one of the abstract
classes in :mod:`providers.base`.  The :mod:`registry` module is responsible
for instantiating the right provider for a given *role* (core / asr / vision)
based on the user's ``models.yaml``.
"""
from .base import (  # noqa: F401
    BaseProvider,
    TextProvider,
    VisionProvider,
    ASRProvider,
    ChatMessage,
    ToolCall,
)
