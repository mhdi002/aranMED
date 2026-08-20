"""HTTP routes for auth, EHR, alerts and education content.

These are mounted from :mod:`app` and all (except auth) require a valid
JWT-style bearer token issued by ``POST /api/auth/login``.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field

import auth
import store
from providers.base import ChatMessage
from registry import Registry

log = logging.getLogger("routes")

router = APIRouter(prefix="/api")


# ===========================================================================
# Auth
# ===========================================================================
class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=256)
    email: Optional[str] = None
    role: str = "doctor"


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


@router.post("/auth/register", response_model=TokenOut)
async def register(body: RegisterIn) -> TokenOut:
    try:
        user = auth.create_user(username=body.username, password=body.password,
                                email=body.email, role=body.role)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    token = auth.create_token({"sub": str(user["id"]),
                               "role": user["role"],
                               "username": user["username"]})
    return TokenOut(access_token=token, user=user)


@router.post("/auth/login", response_model=TokenOut)
async def login(request: Request,
                form: OAuth2PasswordRequestForm = Depends()) -> TokenOut:
    client_ip = request.client.host if request.client else ""
    key = auth.throttle_key(form.username, client_ip)
    locked_for = auth.login_is_locked(key)
    if locked_for > 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"too many failed login attempts — try again in {int(locked_for)}s",
            headers={"Retry-After": str(int(locked_for))},
        )
    user = auth.authenticate(username=form.username, password=form.password)
    if user is None:
        auth.record_login_failure(key)
        # Deliberately identical message for "no such user" and "wrong
        # password" — anything more specific is a user-enumeration oracle.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "invalid username or password")
    auth.record_login_success(key)
    token = auth.create_token({"sub": str(user["id"]),
                               "role": user["role"],
                               "username": user["username"]})
    return TokenOut(access_token=token, user=user)


@router.get("/auth/me")
async def me(user: dict = Depends(auth.current_user)) -> dict:
    return user


# ===========================================================================
# EHR
# ===========================================================================
class BuildEHRIn(BaseModel):
    patient_info: str
    language: str = "en"
    patient_id: Optional[str] = None


@router.post("/ehr/build")
async def ehr_build(body: BuildEHRIn,
                    user: dict = Depends(auth.current_user)) -> dict:
    from tools.ehr import _EHR_SYS_EN, _EHR_SYS_FA, _extract_json

    if not body.patient_info.strip():
        raise HTTPException(400, "patient_info is empty")
    sys = _EHR_SYS_FA if body.language == "fa" else _EHR_SYS_EN
    registry = Registry.get()
    core = await registry.get_text("core")
    out = await core.chat(
        [ChatMessage(role="system", content=sys),
         ChatMessage(role="user", content=body.patient_info)],
        temperature=0.1, max_tokens=1200,
    )
    try:
        data = _extract_json(out.content)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"could not parse EHR JSON: {e}") from e
    record = store.upsert_patient(owner_user_id=user["id"], data=data,
                                  patient_id=body.patient_id,
                                  language=body.language)
    return {"patient_id": record["id"], "record": record}


@router.get("/ehr")
async def ehr_list(user: dict = Depends(auth.current_user)) -> dict:
    return {"records": store.list_patients(owner_user_id=user["id"])}


@router.get("/ehr/{patient_id}")
async def ehr_get(patient_id: str,
                  user: dict = Depends(auth.current_user)) -> dict:
    rec = store.get_patient(patient_id, owner_user_id=user["id"])
    if rec is None:
        raise HTTPException(404, "patient not found")
    return {"patient_id": patient_id, "record": rec}


@router.delete("/ehr/{patient_id}")
async def ehr_delete(patient_id: str,
                     user: dict = Depends(auth.current_user)) -> dict:
    ok = store.delete_patient(patient_id, owner_user_id=user["id"])
    if not ok:
        raise HTTPException(404, "patient not found")
    return {"ok": True}


class RecordDoseIn(BaseModel):
    medication: str
    at: Optional[float] = None


@router.post("/ehr/{patient_id}/dose")
async def ehr_record_dose(patient_id: str, body: RecordDoseIn,
                          user: dict = Depends(auth.current_user)) -> dict:
    import time as _time
    rec = store.get_patient(patient_id, owner_user_id=user["id"])
    if rec is None:
        raise HTTPException(404, "patient not found")
    ts = body.at if body.at is not None else _time.time()
    hit = None
    for med in rec.get("medications", []) or []:
        if (med.get("name") or "").lower() == body.medication.lower():
            med["last_dose_at"] = ts
            hit = med
            break
    if hit is None:
        raise HTTPException(404, f"medication '{body.medication}' not in EHR")
    store.upsert_patient(owner_user_id=user["id"], data=rec,
                         patient_id=patient_id,
                         language=rec.get("language", "en"))
    return {"patient_id": patient_id, "medication": body.medication,
            "last_dose_at": ts}


# ===========================================================================
# Alerts
# ===========================================================================
class CheckIn(BaseModel):
    patient_id: str
    language: str = "en"


@router.post("/alerts/check")
async def alerts_check(body: CheckIn,
                       user: dict = Depends(auth.current_user)) -> dict:
    from tools.alerts import build_alert_text, compute_due_medications
    rec = store.get_patient(body.patient_id, owner_user_id=user["id"])
    if rec is None:
        raise HTTPException(404, "patient not found")
    due = compute_due_medications(rec)
    summary = build_alert_text(rec, due, language=body.language)
    return {"patient_id": body.patient_id, "medications": due,
            "summary": summary,
            "due_count": sum(1 for d in due if d.get("due"))}


class SendAlertIn(BaseModel):
    patient_id: str
    channel: str  # email | sms
    to: str
    language: str = "en"
    only_if_due: bool = True


@router.post("/alerts/send")
async def alerts_send(body: SendAlertIn,
                      user: dict = Depends(auth.current_user)) -> dict:
    from tools.alerts import (build_alert_text, compute_due_medications,
                              send_email, send_sms)
    rec = store.get_patient(body.patient_id, owner_user_id=user["id"])
    if rec is None:
        raise HTTPException(404, "patient not found")
    due = compute_due_medications(rec)
    due_count = sum(1 for d in due if d.get("due"))
    if body.only_if_due and due_count == 0:
        return {"sent": False, "due_count": 0,
                "reason": "no doses currently due"}
    msg = build_alert_text(rec, due, language=body.language)
    name = (rec.get("patient") or {}).get("name") or body.patient_id
    subject = f"[Patient Alert] {name} — {due_count} dose(s) due"
    if body.channel == "email":
        delivery = send_email(to=body.to, subject=subject, body=msg)
    elif body.channel == "sms":
        delivery = send_sms(to=body.to, body=f"{subject}\n\n{msg}")
    else:
        raise HTTPException(400, "channel must be 'email' or 'sms'")
    store.log_alert(patient_id=body.patient_id, channel=body.channel,
                    recipient=body.to, body=msg,
                    dry_run=bool(delivery.get("dry_run")))
    return {"sent": True, "due_count": due_count, "delivery": delivery,
            "body": msg, "subject": subject}


@router.get("/alerts")
async def alerts_list(patient_id: Optional[str] = None,
                      user: dict = Depends(auth.current_user)) -> dict:
    return {"alerts": store.list_alerts(patient_id=patient_id,
                                        owner_user_id=user["id"])}


# ===========================================================================
# Education
# ===========================================================================
class EduIn(BaseModel):
    topic: str
    language: str = "en"
    count: int = 5
    difficulty: str = "intermediate"
    save: bool = True


def _ctx_state() -> dict:
    return {}


async def _run_edu_tool(name: str, **kwargs) -> dict:
    """Helper: invoke a registered tool directly and return its data payload."""
    from tools.base import ToolContext, registry as tool_registry
    registry = Registry.get()
    tool = tool_registry.get(name)
    ctx = ToolContext(registry=registry, state=_ctx_state(),
                      attachments={}, templates=None)
    result = await tool.run(ctx, **kwargs)
    if result.error:
        raise HTTPException(422, result.error)
    return {"content": result.content, "data": result.data}


@router.post("/education/mcq")
async def edu_mcq(body: EduIn,
                  user: dict = Depends(auth.current_user)) -> dict:
    out = await _run_edu_tool("make_mcq", topic=body.topic,
                              language=body.language, count=body.count,
                              difficulty=body.difficulty)
    if body.save and out.get("data"):
        store.save_quiz(owner_user_id=user["id"], kind="mcq",
                        topic=body.topic, language=body.language,
                        data=out["data"])
    return out


@router.post("/education/case")
async def edu_case(body: EduIn,
                   user: dict = Depends(auth.current_user)) -> dict:
    out = await _run_edu_tool("make_case_study", topic=body.topic,
                              language=body.language,
                              difficulty=body.difficulty)
    if body.save and out.get("data"):
        store.save_quiz(owner_user_id=user["id"], kind="case",
                        topic=body.topic, language=body.language,
                        data=out["data"])
    return out


@router.post("/education/exam")
async def edu_exam(body: EduIn,
                   user: dict = Depends(auth.current_user)) -> dict:
    out = await _run_edu_tool("make_mock_exam", topic=body.topic,
                              language=body.language,
                              mcq_count=body.count,
                              difficulty=body.difficulty)
    if body.save and out.get("data"):
        store.save_quiz(owner_user_id=user["id"], kind="exam",
                        topic=body.topic, language=body.language,
                        data=out["data"])
    return out


class ExplainIn(BaseModel):
    concept: str
    language: str = "en"
    level: str = "resident"


@router.post("/education/explain")
async def edu_explain(body: ExplainIn,
                      user: dict = Depends(auth.current_user)) -> dict:
    """Prefer MedicalRAG for grounded explanations; fall back to core LLM."""
    from integrations.medrag_client import MedragClient, MedragError, get_medrag_client

    level = body.level or "resident"
    lang_hint = "Respond in Persian." if body.language == "fa" else "Respond in English."
    query = (
        f"Explain the medical concept '{body.concept}' at the {level} level. "
        f"{lang_hint} Cover definition, mechanism, clinical relevance, and key pearls."
    )
    try:
        client = get_medrag_client()
        payload = await client.ask(query, specialty=None)
        text = MedragClient.format_answer(payload)
        return {
            "content": text,
            "data": {
                "concept": body.concept,
                "language": body.language,
                "explanation": text,
                "source": "medicalrag",
                "medrag": {
                    "sources": payload.get("sources"),
                    "grounding": payload.get("grounding"),
                    "intent": payload.get("intent"),
                },
            },
        }
    except MedragError as e:
        log.warning("MedicalRAG explain unavailable, using core LLM: %s", e)
        try:
            return await _run_edu_tool("explain_concept", concept=body.concept,
                                       language=body.language, difficulty=body.level)
        except Exception as e2:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"MedicalRAG unavailable ({e}); core LLM also failed: {e2}",
            ) from e2


# ===========================================================================
# MedicalRAG knowledge Q&A (microservice adapter)
# ===========================================================================
class KnowledgeAskIn(BaseModel):
    query: str
    specialty: Optional[str] = None


@router.post("/knowledge/ask")
async def knowledge_ask(body: KnowledgeAskIn,
                        user: dict = Depends(auth.current_user)) -> dict:
    """Proxy medical knowledge questions to MedicalRAG POST /ask."""
    from integrations.medrag_client import MedragClient, MedragError, get_medrag_client
    from clinical_safety import enrich_text_payload

    if not body.query.strip():
        raise HTTPException(400, "query is empty")
    try:
        client = get_medrag_client()
        payload = await client.ask(body.query, specialty=body.specialty)
    except MedragError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
    answer = MedragClient.format_answer(payload)
    critical = await enrich_text_payload(
        f"{body.query}\n\n{answer}", use_medrag=False
    )
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
    return {
        "answer": answer,
        "raw": payload,
        "model": "medicalrag",
        "critical_alerts": critical,
    }


class EhrAskIn(BaseModel):
    question: str
    specialty: Optional[str] = None


@router.post("/ehr/{patient_id}/ask")
async def ehr_ask(patient_id: str, body: EhrAskIn,
                  user: dict = Depends(auth.current_user)) -> dict:
    """Q&A about a stored EHR via MedicalRAG (context prepended into query).

    Structured EHR *build* remains on the core LLM (``POST /ehr/build``).
    """
    from integrations.medrag_client import MedragClient, MedragError, get_medrag_client
    import json as _json

    if not body.question.strip():
        raise HTTPException(400, "question is empty")
    rec = store.get_patient(patient_id, owner_user_id=user["id"])
    if rec is None:
        raise HTTPException(404, "patient not found")
    # Strip bookkeeping noise; keep clinical fields for context.
    ctx = {k: v for k, v in rec.items()
           if k not in ("created_at", "updated_at", "owner_user_id")}
    query = (
        "Answer the clinician question using the patient EHR context below. "
        "Do not invent facts not present in the EHR or retrieved knowledge.\n\n"
        f"EHR JSON:\n{_json.dumps(ctx, ensure_ascii=False)}\n\n"
        f"Question: {body.question}"
    )
    try:
        client = get_medrag_client()
        payload = await client.ask(query, specialty=body.specialty)
    except MedragError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
    from clinical_safety import enrich_text_payload

    answer = MedragClient.format_answer(payload)
    critical = await enrich_text_payload(
        f"{body.question}\n\n{answer}", use_medrag=False
    )
    # Persist critical clinical findings onto the EHR alert channel
    if critical:
        try:
            body_txt = "\n".join(
                f"[{a.get('severity')}] {a.get('message')}" for a in critical
            )
            store.log_alert(
                patient_id=patient_id,
                channel="clinical",
                recipient=f"user:{user['id']}",
                body=body_txt,
                dry_run=True,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("clinical alert log failed: %s", e)
    return {
        "patient_id": patient_id,
        "answer": answer,
        "raw": payload,
        "model": "medicalrag",
        "critical_alerts": critical,
    }


@router.get("/rules")
async def rules_list(user: dict = Depends(auth.current_user)) -> dict:
    """Read-only Rule Engine introspection — docs/core/RULE_MODEL_SCHEMA_v1.md.

    Lists every rule known to the backend's Rule Engine (safety bank plus
    the documentation/insurance naming meta-rules); the drug/clinical banks
    are evaluated inside the separate MedicalRAG service and are not
    included here.
    """
    from rules import repository

    rules = repository.all_rules()
    by_bank: dict[str, int] = {}
    for r in rules:
        by_bank[r.bank] = by_bank.get(r.bank, 0) + 1
    return {
        "count": len(rules),
        "by_bank": by_bank,
        "rules": [r.to_dict() for r in rules],
    }


@router.get("/medrag/health")
async def medrag_health(user: dict = Depends(auth.current_user)) -> dict:
    from integrations.medrag_client import MedragError, get_medrag_client

    try:
        client = get_medrag_client()
    except MedragError as e:
        return {"ok": False, "detail": str(e)}
    return await client.health()


@router.get("/education/saved")
async def edu_saved(user: dict = Depends(auth.current_user)) -> dict:
    return {"items": store.list_quizzes(owner_user_id=user["id"])}


@router.get("/education/saved/{quiz_id}")
async def edu_saved_get(quiz_id: int,
                        user: dict = Depends(auth.current_user)) -> dict:
    q = store.get_quiz(quiz_id, owner_user_id=user["id"])
    if q is None:
        raise HTTPException(404, "quiz not found")
    return q
