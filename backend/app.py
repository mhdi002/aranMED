"""FastAPI entry point for the aranmed backend.

The HTTP surface has two layers:

* Legacy single-purpose endpoints (kept for the existing UI):
    GET  /api/health
    GET  /api/templates  + /api/templates/{id}
    POST /api/transcribe (audio → text)
    POST /api/report     (transcript + template → structured report)
    POST /api/dictate    (audio → ASR → structured report)

* Generic agent endpoint (new) that exposes the full tool-calling loop:
    POST /api/chat        (multipart: text + optional audio + optional images)
    GET  /api/models      (registry introspection)
    POST /api/sessions/{id}/reset

* Direct vision endpoint (added) for radiology image understanding:
    POST /api/vision      (prompt + image → description)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import auth
import templates as templates_mod
from agent import Agent
from config import HOST, PORT
from memory import MemoryStore
from providers.base import ChatMessage
from registry import Registry

# ---------------------------------------------------------------------------
# Logging – enable debug for llama.cpp to see vision model loading issues
# ---------------------------------------------------------------------------
logging.getLogger("provider.llamacpp").setLevel(logging.DEBUG)
logging.getLogger("llama_cpp").setLevel(logging.DEBUG)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("api")

app = FastAPI(title="aranmed — Bilingual Radiology Reporter", version="2.0.0")

# Default stays "*" so an on-prem single-origin install works out of the box;
# set CORS_ALLOW_ORIGINS to a comma-separated allowlist for any deployment
# reachable from more than the operator's own browser.
_cors_origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _security_headers(request, call_next):
    """Baseline hardening headers on every response.

    The API serves JSON to a same-origin Next.js frontend, so a restrictive
    default-src costs nothing here and blocks injected content from loading
    anything if a response ever gets rendered directly.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    )
    return response


# ---------------------------------------------------------------------------
# Global exception handler – logs the full traceback, returns an opaque body
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    # Full detail goes to the server log; the client gets a correlation id and
    # nothing else. Echoing str(exc)/type back leaked internals (file paths,
    # driver errors, occasionally query fragments) to anyone who could trigger
    # a 500.
    error_id = uuid.uuid4().hex[:12]
    log.exception("Unhandled exception [%s] in %s", error_id, request.url)
    return JSONResponse(
        status_code=500,
        content={"detail": "internal server error", "error_id": error_id},
    )

# Auth, EHR, alerts and education routes live in their own module.
from routes import router as extra_router  # noqa: E402

app.include_router(extra_router)


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------
registry = Registry.get()
memory = MemoryStore(
    max_sessions=int(registry.runtime.get("memory_max_sessions", 2000)),
    idle_ttl_sec=float(registry.runtime.get("memory_idle_ttl_sec", 6 * 3600)),
)
agent = Agent(registry=registry, memory=memory)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class TranscribeOut(BaseModel):
    text: str
    asr_model: str
    raw_transcript: Optional[str] = None
    language_timeline: Optional[list[dict]] = None


class ReportIn(BaseModel):
    transcript: str
    template_id: Optional[str] = None  # omit to auto-select from the dictation
    extra_context: Optional[str] = None
    model: Optional[str] = None  # provider name override
    patient_id: Optional[str] = None  # optional: attach critical alerts to EHR


class ReportOut(BaseModel):
    report: str
    template_id: str
    model: str
    critical_alerts: list[dict] = []
    template_mismatch: Optional[dict] = None
    auto_selected_template: bool = False
    template_selection_confidence: Optional[float] = None
    template_selection_reason: Optional[str] = None


class TemplateSuggestIn(BaseModel):
    transcript: str
    top_k: int = 3


class TemplateSuggestOut(BaseModel):
    suggestions: list[dict]


class ChatOut(BaseModel):
    session_id: str
    answer: str
    tool_calls: list[dict]
    state: dict
    model: str
    critical_alerts: list[dict] = []


class VisionOut(BaseModel):
    answer: str
    model: str
    critical_alerts: list[dict] = []


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
_purge_task: Optional[asyncio.Task] = None


async def _purge_stale_sessions_loop() -> None:
    """Periodically drop agent_sessions rows past their idle TTL.

    In-process eviction (MemoryStore._evict_if_needed) only bounds the cache;
    without this the durable table grows for the lifetime of the deployment.
    Interval is config-driven; set memory_purge_interval_sec to 0 to disable
    (e.g. if an external cron owns the purge instead).
    """
    interval = float(registry.runtime.get("memory_purge_interval_sec", 3600))
    if interval <= 0:
        log.info("memory purge loop disabled (memory_purge_interval_sec=%s)", interval)
        return
    while True:
        try:
            await asyncio.sleep(interval)
            await asyncio.to_thread(memory.purge_stale)
            # Revocation rows for tokens that have expired anyway are dead
            # weight — an expired token is rejected on its own merits.
            await asyncio.to_thread(auth.purge_expired_revocations)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("memory purge loop iteration failed; continuing")


@app.on_event("startup")
async def _startup() -> None:
    templates_mod.initialise()
    # Seed default admin (admin/admin) on first start.
    from auth import ensure_default_admin

    ensure_default_admin()
    global _purge_task
    _purge_task = asyncio.create_task(_purge_stale_sessions_loop())
    log.info(
        "registry roles=%s defaults=%s",
        list(registry._role_index.keys()),  # noqa: SLF001
        registry._defaults,  # noqa: SLF001
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _purge_task is not None:
        _purge_task.cancel()


# ---------------------------------------------------------------------------
# Health / introspection
# ---------------------------------------------------------------------------
@app.get("/api/live")
async def live() -> dict:
    """Liveness only — touches no dependency, so it stays fast and truthful
    under load. Load balancers and container healthchecks should probe this;
    ``/api/health`` is the deeper readiness view. See docs/core/DEPLOYMENT.md.
    """
    return {"ok": True, "service": "aranmed"}


# `/api/health` fans out to Ollama and MedicalRAG. Doing that per request melts
# down under concurrency (measured: p50 17s at 50 users, with timeouts), so the
# dependency probe is cached for a short TTL and de-duplicated: N concurrent
# callers trigger at most one upstream probe. Tune with HEALTH_CACHE_TTL_SEC.
_HEALTH_TTL = float(os.getenv("HEALTH_CACHE_TTL_SEC", "5"))
_health_cache: dict[str, Any] = {"at": 0.0, "value": None}
_health_lock = asyncio.Lock()


async def _probe_dependencies() -> dict:
    """Actually contact Ollama + MedicalRAG. Callers must go through
    :func:`_dependency_health` so this runs at most once per TTL."""
    info = registry.list()
    core_default = info["defaults"].get("core")
    core_health: dict = {}
    if core_default:
        try:
            core_health = await registry._providers[core_default].health()  # noqa: SLF001
        except Exception as e:  # noqa: BLE001
            core_health = {"ok": False, "detail": str(e)}

    medrag_health: dict = {"ok": False, "detail": "not checked"}
    try:
        from integrations.medrag_client import MedragClient, MedragError

        url = (os.getenv("MEDRAG_API_URL") or os.getenv("MEDICALRAG_URL") or "").strip().rstrip("/")
        if not url:
            medrag_health = {"ok": False, "detail": "MEDRAG_API_URL unset"}
        else:
            try:
                timeout = float(os.getenv("MEDRAG_HEALTH_TIMEOUT_SEC", "3"))
                medrag_health = await MedragClient(base_url=url, timeout_sec=timeout).health()
            except MedragError as e:
                medrag_health = {"ok": False, "detail": str(e)}
    except Exception as e:  # noqa: BLE001
        medrag_health = {"ok": False, "detail": str(e)}

    return {"core_health": core_health, "medrag": medrag_health}


async def _dependency_health() -> dict:
    now = time.monotonic()
    cached = _health_cache["value"]
    if cached is not None and (now - _health_cache["at"]) < _HEALTH_TTL:
        return cached
    async with _health_lock:
        # Re-check inside the lock: whoever waited gets the fresh value for free.
        now = time.monotonic()
        cached = _health_cache["value"]
        if cached is not None and (now - _health_cache["at"]) < _HEALTH_TTL:
            return cached
        value = await _probe_dependencies()
        _health_cache["value"] = value
        _health_cache["at"] = time.monotonic()
        return value


@app.get("/api/health")
async def health() -> dict:
    """Readiness probe for the front-end pills (dependency results cached)."""
    info = registry.list()
    core_default = info["defaults"].get("core")
    asr_default = info["defaults"].get("asr")
    vision_default = info["defaults"].get("vision")

    deps = await _dependency_health()
    core_health = deps["core_health"]
    medrag_health = deps["medrag"]

    # core_default is the registry *slot name* (e.g. "ollama-core"), not the
    # actual model — resolve to what the provider's own health check reports
    # is loaded, falling back to the slot name only if that's unavailable.
    resolved_core_model = core_health.get("model") or core_default

    return {
        "ok": True,
        "service": "aranmed",
        "asr_model": asr_default,
        "ollama_model": resolved_core_model,  # legacy field name
        "core_model": resolved_core_model,
        "core_slot": core_default,
        "vision_model": vision_default,
        "ollama_available": bool(core_health.get("ok")),
        "ollama_models": core_health.get("available_models", []),
        "medrag": medrag_health,
        "vram_used_gb": info["vram_used_gb"],
    }


# Registry introspection fans out to every provider's health endpoint, so it
# carries the same cost profile as /api/health and gets the same treatment:
# TTL cache + single-flight. Without it an admin page polling this endpoint
# hammers Ollama/Triton once per request.
#
# Note the TTL is deliberately longer than the health TTL. Caches are
# per-worker, so with N uvicorn workers a given worker only sees every Nth
# request; a 5 s TTL expires before the round-robin comes back around and the
# cache never hits. Registry composition only changes on model load/unload,
# so a longer window is both safe and effective.
_MODELS_TTL = float(os.getenv("MODELS_CACHE_TTL_SEC", "30"))
_models_cache: dict[str, Any] = {"at": 0.0, "value": None}
_models_lock = asyncio.Lock()


@app.get("/api/models")
async def models() -> dict:
    now = time.monotonic()
    cached = _models_cache["value"]
    if cached is not None and (now - _models_cache["at"]) < _MODELS_TTL:
        return cached
    async with _models_lock:
        now = time.monotonic()
        cached = _models_cache["value"]
        if cached is not None and (now - _models_cache["at"]) < _MODELS_TTL:
            return cached
        value = await registry.health()
        _models_cache["value"] = value
        _models_cache["at"] = time.monotonic()
        return value




# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
@app.get("/api/templates")
async def templates_list() -> dict:
    return {"templates": templates_mod.list_templates()}


@app.get("/api/templates/{template_id}")
async def template_get(template_id: str) -> dict:
    try:
        body = templates_mod.get_template(template_id)
    except FileNotFoundError as e:
        raise HTTPException(404, f"template not found: {e}") from e
    return {"id": template_id, "body": body}


# ---------------------------------------------------------------------------
# Single-purpose endpoints
# ---------------------------------------------------------------------------
@app.post("/api/transcribe", response_model=TranscribeOut)
async def transcribe(
    file: UploadFile = File(...),
    language: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
) -> TranscribeOut:
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty upload")
    try:
        asr = await registry.get_asr(name=model)
        text = await asr.transcribe(
            raw, language=language, filename_hint=file.filename or ""
        )
        raw_transcript = getattr(asr, "last_raw_transcript", None) or None
        language_timeline = getattr(asr, "last_timeline", None) or None
        log.info("Transcription result: %s", text[:200])
    except Exception as e:  # noqa: BLE001
        log.exception("ASR failed")
        raise HTTPException(500, f"ASR failed: {e}") from e
    return TranscribeOut(
        text=text,
        asr_model=asr.name,
        raw_transcript=raw_transcript,
        language_timeline=language_timeline,
    )


@app.post("/api/templates/suggest", response_model=TemplateSuggestOut)
async def suggest_template(req: TemplateSuggestIn) -> TemplateSuggestOut:
    import template_selection

    suggestions = template_selection.suggest_templates(req.transcript, top_k=req.top_k)
    return TemplateSuggestOut(suggestions=[s.to_dict() for s in suggestions])


@app.post("/api/report", response_model=ReportOut)
async def report(req: ReportIn) -> ReportOut:
    template_id = req.template_id
    auto_selected = False
    selection_confidence: Optional[float] = None
    selection_reason: Optional[str] = None
    if not (template_id or "").strip():
        import template_selection

        pick = template_selection.auto_select_template(req.transcript)
        if pick is None:
            raise HTTPException(
                422,
                "no template_id was provided and automatic selection was not "
                "confident enough — please select a template",
            )
        template_id = pick.template_id
        auto_selected = True
        selection_confidence = pick.confidence
        selection_reason = pick.detail or pick.matched_on

    try:
        tpl = templates_mod.get_template(template_id)
        meta = templates_mod.get_template_meta(template_id)
    except FileNotFoundError:
        raise HTTPException(404, "template not found")
    from tools.builtin import _STRUCTURE_SYS  # type: ignore[attr-defined]
    from clinical_safety import enrich_report_payload
    import report_rules as rules_mod

    template_title = meta.get("title") or rules_mod.official_title_for(template_id)
    medrag_ans = await rules_mod.consult_medrag_naming_rules(
        template_title=template_title, transcript=req.transcript,
    )
    merged_ctx = rules_mod.build_report_context(
        template_title=template_title,
        transcript=req.transcript,
        extra_context=req.extra_context,
        medrag_answer=medrag_ans,
    )

    core = await registry.get_text("core", name=req.model)
    prompt = (
        f"=== TEMPLATE (id={template_id}; title={template_title or 'unknown'}) ===\n"
        f"{tpl}\n\n"
        f"=== DICTATION ===\n{req.transcript}\n\n"
        "NOTE: Fill the TEMPLATE above exactly. The UI-selected template wins "
        "over any exam/template name spoken in the dictation.\n\n"
    )
    if merged_ctx:
        prompt += f"=== ADDITIONAL CONTEXT ===\n{merged_ctx}\n\n"
    prompt += "=== FILLED REPORT ===\n"
    out = await core.chat(
        [
            ChatMessage(role="system", content=_STRUCTURE_SYS),
            ChatMessage(role="user", content=prompt),
        ],
        temperature=0.2,
        max_tokens=1400,
    )
    safety = await enrich_report_payload(
        transcript=req.transcript,
        report_text=out.content,
        template_id=template_id,
        patient_id=req.patient_id,
    )
    return ReportOut(
        report=out.content,
        template_id=template_id,
        model=core.name,
        critical_alerts=safety.get("critical_alerts") or [],
        template_mismatch=safety.get("template_mismatch"),
        auto_selected_template=auto_selected,
        template_selection_confidence=selection_confidence,
        template_selection_reason=selection_reason,
    )


@app.post("/api/dictate", response_model=ReportOut)
async def dictate(
    file: UploadFile = File(...),
    template_id: Optional[str] = Form(None),
    language: Optional[str] = Form(None),
    extra_context: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
) -> ReportOut:
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty upload")
    asr = await registry.get_asr()
    try:
        transcript = await asr.transcribe(
            raw, language=language, filename_hint=file.filename or ""
        )
    except Exception as e:  # noqa: BLE001
        log.exception("ASR failed")
        raise HTTPException(500, f"ASR failed: {e}") from e
    return await report(
        ReportIn(
            transcript=transcript,
            template_id=template_id,
            extra_context=extra_context,
            model=model,
        )
    )


# ---------------------------------------------------------------------------
# Generic agent endpoint (modified: uses vision model directly if images present)
# ---------------------------------------------------------------------------
@app.post("/api/chat", response_model=ChatOut)
async def chat(
    text: str = Form(""),
    session_id: Optional[str] = Form(None),
    core_model: Optional[str] = Form(None),
    audio: Optional[list[UploadFile]] = File(None),
    images: Optional[list[UploadFile]] = File(None),
    user: Optional[dict] = Depends(auth.current_user_optional),
) -> ChatOut:
    sid = session_id or uuid.uuid4().hex
    attachments: dict[str, bytes] = {}
    for i, f in enumerate(audio or []):
        attachments[f"audio:{i}"] = await f.read()
    for i, f in enumerate(images or []):
        attachments[f"image:{i}"] = await f.read()

    if not text and not attachments:
        raise HTTPException(400, "either text or audio/image attachments required")

    # Text-only medical knowledge → MedicalRAG microservice (no local LLM rewrite).
    # Structured report / tool-calling agent still handles audio + template flows.
    if text and not audio and not images:
        from integrations.medrag_client import MedragClient, MedragError, get_medrag_client

        try:
            from clinical_safety import enrich_text_payload

            payload = await get_medrag_client().ask(text, specialty="radiology")
            answer = MedragClient.format_answer(payload)
            critical = await enrich_text_payload(
                f"{text}\n\n{answer}", use_medrag=False
            )
            # Prefer MedRAG rule_alerts when present
            for a in payload.get("rule_alerts") or []:
                if isinstance(a, dict):
                    critical.append(
                        {
                            "code": a.get("code") or "medrag_rule",
                            "severity": a.get("severity") or "high",
                            "label": a.get("label") or "MedRAG rule alert",
                            "excerpt": (a.get("excerpt") or "")[:160],
                            "source": "medrag_rule_alerts",
                            "message": a.get("message") or str(a),
                        }
                    )
                else:
                    critical.append(
                        {
                            "code": "medrag_rule",
                            "severity": "high",
                            "label": "MedRAG rule alert",
                            "excerpt": "",
                            "source": "medrag_rule_alerts",
                            "message": str(a),
                        }
                    )
            return ChatOut(
                session_id=sid,
                answer=answer,
                tool_calls=[],
                state={"medrag": True, "sources": payload.get("sources")},
                model="medicalrag",
                critical_alerts=critical,
            )
        except MedragError as e:
            log.warning("MedicalRAG text chat unavailable: %s", e)
            raise HTTPException(
                status_code=503,
                detail=f"MedicalRAG unavailable for knowledge chat: {e}",
            ) from e

    # Image-only chat: bypass the agent and query the vision model directly.
    # When audio is also attached the full agent runs instead, so the ASR tool
    # can process the audio alongside describe_image.
    if images and not audio:
        log.info(f"Using vision model for chat request (session {sid})")
        vision = await registry.get_vision(name=core_model)  # core_model overrides vision if supplied
        # Use the first image (or could iterate, but agent typically expects one)
        img_bytes = attachments.get("image:0")
        if not img_bytes:
            raise HTTPException(400, "Image upload failed")
        msg = ChatMessage(
            role="user",
            content=text or "Describe this radiology image in detail.",
            images=[img_bytes]
        )
        try:
            resp = await vision.chat([msg])
        except Exception as e:
            log.exception("Vision chat failed")
            raise HTTPException(500, f"Vision model error: {e}")
        from clinical_safety import enrich_text_payload

        critical = await enrich_text_payload(
            f"{text or ''}\n\n{resp.content}", use_medrag=False
        )
        return ChatOut(
            session_id=sid,
            answer=resp.content,
            tool_calls=[],
            state={"vision_used": True},
            model=vision.name,
            critical_alerts=critical,
        )

    # Otherwise, run the standard agent (text + audio only)
    res = await agent.run(
        session_id=sid,
        user_text=text or "(no text)",
        attachments=attachments,
        core_name=core_model,
        owner_user_id=(user or {}).get("id"),
    )
    from clinical_safety import enrich_text_payload

    critical = list(res.state.get("critical_alerts") or [])
    if not critical:
        critical = await enrich_text_payload(
            f"{text or ''}\n\n{res.answer}", use_medrag=False
        )
    return ChatOut(
        session_id=sid,
        answer=res.answer,
        tool_calls=res.tool_calls,
        state=res.state,
        model=res.model,
        critical_alerts=critical,
    )


@app.post("/api/sessions/{session_id}/reset")
async def reset_session(session_id: str) -> dict:
    memory.reset(session_id)
    return {"ok": True, "session_id": session_id}


# ---------------------------------------------------------------------------
# Direct vision endpoint – uses the registered vision model (radiology-infer-mini)
# ---------------------------------------------------------------------------
@app.post("/api/vision", response_model=VisionOut)
async def vision_endpoint(
    prompt: Optional[str] = Form(None),
    image: UploadFile = File(...),
    model: Optional[str] = Form(None),
) -> VisionOut:
    """Analyse a single radiology image using the vision model."""
    if not prompt:
        prompt = "Describe this radiology image in detail."

    log.info(f"Vision request: prompt='{prompt[:50]}...', image={image.filename}, model={model}")

    img_bytes = await image.read()
    if not img_bytes:
        raise HTTPException(400, "Uploaded image is empty")

    vision = await registry.get_vision(name=model)
    msg = ChatMessage(role="user", content=prompt, images=[img_bytes])

    try:
        resp = await vision.chat([msg])
    except Exception as e:
        log.exception("Vision inference failed")
        raise HTTPException(500, f"Vision error: {e}")

    from clinical_safety import enrich_text_payload

    critical = await enrich_text_payload(
        f"{prompt}\n\n{resp.content}", use_medrag=False
    )
    return VisionOut(
        answer=resp.content, model=vision.name, critical_alerts=critical
    )


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)