"""LLM specialty, language, and clinical intent routing.

Supports:
  - vLLM / OpenAI-compatible  (/v1/chat/completions) — provider=vllm|openai
    Base URL: MEDRAG_LLM_BASE_URL / VLLM_BASE_URL (e.g. http://localhost:8000/v1)
  - Ollama native             (/api/chat)            — provider=ollama

Connection pooling via a shared requests.Session for concurrent RAG requests.
Optional fallback_provider (typically ollama) when the primary backend is down.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from medrag.config import (
    INTENT_ROUTING,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_ENABLE_THINKING,
    LLM_FALLBACK_ENDPOINT,
    LLM_FALLBACK_MODEL,
    LLM_FALLBACK_PROVIDER,
    LLM_MAX_RETRIES,
    LLM_MAX_TOKENS,
    LLM_MODEL,
    LLM_NUM_CTX,
    LLM_PROVIDER,
    LLM_STRIP_THINKING,
    LLM_TEMPERATURE,
    LLM_TIMEOUT,
    LLM_TOP_P,
    OLLAMA_URL,
)
from medrag.catalog.registry import all_specialties

log = logging.getLogger(__name__)

INTENT_LABELS = ("clinical", "drug", "legal", "coding", "general")

# Qwen3 / Qwen3.5 thinking blocks (and common variants)
_THINK_RE = re.compile(
    r"<think>.*?</think>|"
    r"<\|thinking\|>.*?<\|/thinking\|>|"
    r"</?think>",
    re.DOTALL | re.IGNORECASE,
)
_COT_LEAD_RE = re.compile(
    r"^(?:The user is asking|I need to extract|Analysis of the context|"
    r"Let me (?:think|analyze)|I'll (?:analyze|reason)|"
    r"Thinking Process:|Thinking:|\*\*Thinking\*\*)"
    r".*?(?=\n\n(?=[A-Z\u0600-\u06FF\*•-]|\*\*)|\Z)",
    re.DOTALL | re.IGNORECASE,
)
# Plain-text thinking dump (Windows HF server / some templates)
_THINKING_PROCESS_RE = re.compile(
    r"(?:^|\n)\s*(?:Thinking Process:|Thinking:|\*\*Thinking(?:\s+Process)?\*\*)\s*\n"
    r".*?"
    r"(?=\n\n(?:Based on|According to|Sources used|\*\*[A-Z]|[\u0600-\u06FF])|\Z)",
    re.DOTALL | re.IGNORECASE,
)

_INTENT_HINTS = [
    ("legal", (
        r"آیین.?نامه|قانون|ماده\s+\d+|حقوق|مجوز|استاندارد\s*ملی|"
        r"\bSOP\b|regulation|statute|legal\b|malpractice|بیمه\s*تکمیلی"
    )),
    ("coding", (
        r"\bICD[-\s]?\d*\b|\bCPT\b|\bLOINC\b|کد\s*گذاری|کدینگ|coding|diagnosis\s*code"
    )),
    ("drug", (
        r"دارو|دوز|تداخل|حساسیت|متفورمین|وارفارین|آمیودارون|"
        r"dose|drug|medication|contraindicat|interact|allergy|metformin|warfarin"
    )),
    ("clinical", (
        r"بیمار|تشخیص|علائم|درمان|گایدلاین|guideline|symptom|diagnos|"
        r"treatment|eGFR|CKD|NSTEMI|sepsis|سپسیس"
    )),
]


class LLMUnavailableError(RuntimeError):
    """Raised when the configured LLM backend (and optional fallback) cannot be reached."""


def strip_thinking(text: str | None) -> str:
    """Remove Qwen CoT / <think> blocks so answers stay citation-clean."""
    if not text:
        return ""
    if not LLM_STRIP_THINKING:
        return text.strip()
    out = _THINK_RE.sub("", text)
    # Incomplete stream: drop everything up to closing think tag
    if "</think>" in out.lower():
        parts = re.split(r"</think>", out, flags=re.I)
        out = parts[-1]
    if "<think>" in out.lower():
        out = re.split(r"<think>", out, flags=re.I)[0]
    out = _THINKING_PROCESS_RE.sub("\n", out)
    out = out.strip()
    # If the whole reply is still a thinking dump truncated mid-sentence, reject it
    if re.match(r"^(?:Thinking Process:|Thinking:)", out, re.I):
        # Keep only content after a clear final-answer marker if present
        m = re.search(
            r"\n\n((?:Based on|According to|Sources used|\*\*|Final answer:).*)",
            out,
            re.I | re.S,
        )
        if m:
            out = m.group(1).strip()
        else:
            # No recoverable answer — empty so Self-RAG / caller can retry
            return ""
    # Heuristic: leaked planning prose without tags (seen with HF chat template)
    if out and _COT_LEAD_RE.match(out):
        chunks = re.split(r"\n{2,}", out)
        for i, ch in enumerate(chunks):
            if re.match(
                r"^(?:Based on|According to|\*\*|Sources used|Final answer:|"
                r"[\u0600-\u06FF]|ECG|The (?:earliest|ECG|main|first|correct)|"
                r"In |Metformin|Warfarin|Peaked)",
                ch.strip(),
                re.I,
            ):
                out = "\n\n".join(chunks[i:]).strip()
                break
        else:
            for i, ch in enumerate(chunks):
                if i == 0 and _COT_LEAD_RE.match(ch):
                    continue
                if len(ch.strip()) > 40:
                    out = "\n\n".join(chunks[i:]).strip()
                    break
    return out.strip()


def _make_session() -> requests.Session:
    """Pooled HTTP session — safe for multi-request / concurrent callers."""
    session = requests.Session()
    retries = Retry(
        total=max(0, LLM_MAX_RETRIES),
        connect=max(0, LLM_MAX_RETRIES),
        read=0,  # do not retry long LLM reads
        backoff_factor=0.4,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


_SESSION = _make_session()


def _openai_base(url: str) -> str:
    """Normalize to .../v1 for chat.completions. Empty URL is a config error."""
    u = (url or "").rstrip("/")
    if not u:
        raise ValueError(
            "LLM_BASE_URL is empty — set llm.base_url in config.yaml or "
            "MEDRAG_LLM_BASE_URL / VLLM_BASE_URL"
        )
    if u.endswith("/v1"):
        return u
    if u.endswith("/chat/completions"):
        return u[: -len("/chat/completions")]
    # Bare host:port → append /v1 (vLLM default)
    if re.search(r":\d+$", u) or u.count("/") <= 2:
        return u + "/v1"
    return u


def _friendly_unreachable(provider: str, url: str, err: BaseException) -> str:
    hints = {
        "vllm": (
            f"vLLM is unreachable at {url}. "
            "Start it (docker compose --profile vllm up -d, or "
            "`vllm serve <model>` with VLLM_PORT) and set "
            "MEDRAG_LLM_BASE_URL / VLLM_BASE_URL."
        ),
        "openai": (
            f"OpenAI-compatible endpoint unreachable at {url}. "
            "Check MEDRAG_LLM_BASE_URL and MEDRAG_LLM_API_KEY."
        ),
        "ollama": (
            f"Ollama is unreachable at {url}. "
            "Start Ollama and ensure MEDRAG_OLLAMA_URL / OLLAMA_HOST is correct."
        ),
    }
    base = hints.get(provider, f"LLM provider '{provider}' unreachable at {url}.")
    return f"{base} Underlying error: {type(err).__name__}: {err}"


def _openai_compatible_chat(
    messages,
    *,
    model: str,
    base_url: str,
    api_key: str | None = None,
    temperature: float | None = None,
    fmt: str | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
    enable_thinking: bool | None = None,
) -> str:
    """POST /v1/chat/completions — works with vLLM, OpenAI, LM Studio, etc."""
    base = _openai_base(base_url)
    temp = LLM_TEMPERATURE if temperature is None else temperature
    think = LLM_ENABLE_THINKING if enable_thinking is None else enable_thinking
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temp,
        "top_p": LLM_TOP_P,
        "max_tokens": max_tokens if max_tokens is not None else LLM_MAX_TOKENS,
        "stream": False,
        # Qwen3.5 / vLLM: disable default thinking mode for clean RAG answers
        "chat_template_kwargs": {"enable_thinking": think},
        "enable_thinking": think,
    }
    if fmt == "json":
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or 'EMPTY'}",
    }
    url = f"{base}/chat/completions"
    try:
        # Short connect timeout so a down primary fails fast into fallback.
        to = timeout if timeout is not None else (5.0, float(LLM_TIMEOUT))
        r = _SESSION.post(url, json=payload, headers=headers, timeout=to)
        r.raise_for_status()
    except requests.RequestException as e:
        raise LLMUnavailableError(_friendly_unreachable("vllm", base, e)) from e
    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    # Prefer final content; never return reasoning_content as the answer
    content = msg.get("content") or ""
    if not content and msg.get("reasoning_content") and think:
        content = msg.get("reasoning_content") or ""
    return strip_thinking(content)


def _ollama_native_chat(
    messages,
    *,
    model: str,
    endpoint: str,
    temperature: float | None = None,
    fmt: str | None = None,
    think: bool | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
) -> str:
    temp = LLM_TEMPERATURE if temperature is None else temperature
    use_think = LLM_ENABLE_THINKING if think is None else think
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": use_think,
        "options": {
            "temperature": temp,
            "num_ctx": LLM_NUM_CTX,
            "num_predict": max_tokens if max_tokens is not None else LLM_MAX_TOKENS,
            "top_p": LLM_TOP_P,
        },
    }
    if fmt:
        payload["format"] = fmt
    url = f"{endpoint.rstrip('/')}/api/chat"
    try:
        r = _SESSION.post(url, json=payload, timeout=timeout or LLM_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        raise LLMUnavailableError(_friendly_unreachable("ollama", endpoint, e)) from e
    msg = r.json()["message"]
    content = msg.get("content") or ""
    if not content and use_think:
        content = msg.get("thinking") or ""
    return strip_thinking(content)


def _chat_with_provider(
    provider: str,
    messages,
    *,
    model: str | None = None,
    endpoint: str | None = None,
    temperature: float | None = None,
    fmt: str | None = None,
    think: bool | None = None,
    max_tokens: int | None = None,
) -> str:
    provider = (provider or "vllm").lower().strip()
    if provider in ("vllm", "openai"):
        return _openai_compatible_chat(
            messages,
            model=model or LLM_MODEL,
            base_url=endpoint or LLM_BASE_URL,
            api_key=LLM_API_KEY,
            temperature=temperature,
            fmt=fmt,
            max_tokens=max_tokens,
            enable_thinking=think,
        )
    # ollama (default for unknown → treat as ollama native for safety when fallback)
    return _ollama_native_chat(
        messages,
        model=model or LLM_MODEL,
        endpoint=endpoint or OLLAMA_URL,
        temperature=temperature,
        fmt=fmt,
        think=think,
        max_tokens=max_tokens,
    )


def llm_health(provider: str | None = None) -> dict:
    """Lightweight readiness probe for the active (or named) provider."""
    provider = (provider or LLM_PROVIDER).lower().strip()
    try:
        if provider in ("vllm", "openai"):
            base = _openai_base(LLM_BASE_URL)
            r = _SESSION.get(f"{base}/models", timeout=5)
            ok = r.status_code < 500
            return {"provider": provider, "ok": ok, "url": base, "status": r.status_code}
        url = f"{OLLAMA_URL.rstrip('/')}/api/tags"
        r = _SESSION.get(url, timeout=5)
        return {"provider": "ollama", "ok": r.ok, "url": OLLAMA_URL, "status": r.status_code}
    except requests.RequestException as e:
        return {"provider": provider, "ok": False, "error": str(e)}


# After primary fails once, skip straight to fallback for this process lifetime.
_primary_down: bool = False


def ollama_chat(messages, temperature=None, fmt=None, think=None, max_tokens=None):
    """Chat entrypoint — dispatches by LLM_PROVIDER with optional fallback.

    Kept name for backward compatibility; prefer ``llm_chat``.
    ``think`` defaults to config ``llm.enable_thinking`` (False for RAG).
    """
    global _primary_down
    primary = LLM_PROVIDER or "vllm"
    fb = LLM_FALLBACK_PROVIDER
    use_think = LLM_ENABLE_THINKING if think is None else think

    if _primary_down and fb and fb != primary:
        return _chat_with_provider(
            fb,
            messages,
            model=LLM_FALLBACK_MODEL or LLM_MODEL,
            endpoint=LLM_FALLBACK_ENDPOINT or OLLAMA_URL,
            temperature=temperature,
            fmt=fmt,
            think=use_think,
            max_tokens=max_tokens,
        )

    try:
        return _chat_with_provider(
            primary, messages, temperature=temperature, fmt=fmt,
            think=use_think, max_tokens=max_tokens,
        )
    except LLMUnavailableError as primary_err:
        if not fb or fb == primary:
            raise
        _primary_down = True
        log.warning(
            "Primary LLM (%s) unavailable; falling back to %s (circuit open): %s",
            primary, fb, primary_err,
        )
        try:
            return _chat_with_provider(
                fb,
                messages,
                model=LLM_FALLBACK_MODEL or LLM_MODEL,
                endpoint=LLM_FALLBACK_ENDPOINT or OLLAMA_URL,
                temperature=temperature,
                fmt=fmt,
                think=use_think,
                max_tokens=max_tokens,
            )
        except LLMUnavailableError as fb_err:
            raise LLMUnavailableError(
                f"{primary_err} Fallback ({fb}) also failed: {fb_err}"
            ) from fb_err


# Explicit alias for new call sites
llm_chat = ollama_chat


def detect_language(text: str) -> str:
    fa_chars = len(re.findall(r"[\u0600-\u06FF]", text))
    return "fa" if fa_chars > len(text) * 0.15 else "en"


def classify_intent(query: str) -> dict:
    """Return {intent, filters} for retrieval. Heuristic-first; LLM optional."""
    if not INTENT_ROUTING:
        return {"intent": "general", "filters": {}}

    for label, pat in _INTENT_HINTS:
        if re.search(pat, query, re.I):
            return {"intent": label, "filters": _filters_for_intent(label)}

    try:
        out = ollama_chat(
            [{"role": "user", "content": (
                "Classify the medical query intent. Return JSON "
                '{"intent": one of clinical|drug|legal|coding|general}.\n\n'
                f"Query: {query}"
            )}],
            fmt="json", temperature=0,
        )
        intent = json.loads(out).get("intent", "general")
        if intent not in INTENT_LABELS:
            intent = "general"
    except Exception:
        intent = "general"
    return {"intent": intent, "filters": _filters_for_intent(intent)}


def _filters_for_intent(intent: str) -> dict:
    if intent == "legal":
        # Soft filters: corpus + country only (AND of tags+types was too strict)
        return {
            "source_corpus": ["standards"],
            "country": "IR",
        }
    if intent == "drug":
        return {
            "index_tags": ["drug", "guideline", "textbook", "standard"],
        }
    if intent == "coding":
        return {
            "source_corpus": ["standards", "library", "mehrsys"],
        }
    if intent == "clinical":
        return {
            "doc_types": ["guideline", "textbook", "standard", "exam", "qbank"],
        }
    return {}


def route_specialties(query: str) -> list[str] | None:
    specs = all_specialties()
    if not specs:
        return None
    prompt = (
        "You are a medical query router. Return 1-3 most relevant specialties "
        'as JSON {"specialties": [...]}. Only use names from the list.\n\n'
        f"List: {specs}\n\nQuestion: {query}")
    try:
        out = ollama_chat([{"role": "user", "content": prompt}], fmt="json", temperature=0)
        picked = json.loads(out).get("specialties", [])
        return [s for s in picked if s in specs] or None
    except Exception:
        return None
