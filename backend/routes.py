"""HTTP routes for auth, EHR, alerts and education content.

These are mounted from :mod:`app` and all (except auth) require a valid
JWT-style bearer token issued by ``POST /api/auth/login``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import (APIRouter, Depends, File, HTTPException, Request, status,
                     UploadFile)
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field

import audit
import auth
import mfa
import rbac
import store
from providers.base import ChatMessage
from registry import Registry

log = logging.getLogger("routes")

router = APIRouter(prefix="/api")


def _ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


# ===========================================================================
# Auth
# ===========================================================================
class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    # Length policy is enforced in auth.validate_password (configurable per
    # deployment); keep the schema bound generous so the API returns the
    # specific policy message rather than a generic 422.
    password: str = Field(min_length=1, max_length=1024)
    email: Optional[str] = None
    # Omitted -> the first self-registrable role. Anything outside
    # SELF_REGISTER_ROLES needs an authenticated caller with users.manage.
    role: Optional[str] = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


def self_register_roles() -> list[str]:
    """Roles an anonymous visitor may give themselves (``SELF_REGISTER_ROLES``,
    comma-separated, default ``student``). Empty disables self-registration."""
    raw = os.environ.get("SELF_REGISTER_ROLES", "student")
    return [r.strip().lower() for r in raw.split(",") if r.strip()]


@router.get("/auth/register-policy")
async def register_policy() -> dict:
    return {"self_register_roles": self_register_roles()}


@router.post("/auth/register", response_model=TokenOut)
async def register(request: Request, body: RegisterIn,
                   caller: Optional[dict] = Depends(auth.current_user_optional)) -> TokenOut:
    allowed = self_register_roles()
    role = (body.role or (allowed[0] if allowed else "")).strip().lower()
    is_manager = bool(caller) and rbac.allows((caller.get("role") or "").strip().lower(),
                                              "users.manage")
    if role not in allowed and not is_manager:
        audit.record("auth.register", actor=caller, actor_name=None if caller else body.username,
                     outcome="deny", client_ip=_ip(request),
                     detail={"reason": "role_not_self_registrable", "role": role})
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"accounts with role '{role}' are created by an administrator"
            if role else "self-registration is disabled — ask an administrator")
    try:
        user = auth.create_user(username=body.username, password=body.password,
                                email=body.email, role=role)
    except ValueError as e:
        audit.record("auth.register", actor_name=body.username, outcome="deny",
                     client_ip=_ip(request), detail={"reason": str(e)})
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    audit.record("auth.register", actor=caller or user, client_ip=_ip(request),
                 detail={"role": user["role"], "user": user["username"],
                         "created_by_admin": is_manager and caller is not None})
    token = auth.create_token({"sub": str(user["id"]),
                               "role": user["role"],
                               "username": user["username"]})
    return TokenOut(access_token=token, user=user)


@router.get("/auth/users")
async def list_users(user: dict = Depends(rbac.require("users.manage"))) -> dict:
    return {"users": auth.list_users(), "self_register_roles": self_register_roles()}


@router.post("/auth/login", response_model=TokenOut)
async def login(request: Request,
                form: OAuth2PasswordRequestForm = Depends()) -> TokenOut:
    client_ip = _ip(request) or ""
    key = auth.throttle_key(form.username, client_ip)
    locked_for = auth.login_is_locked(key)
    if locked_for > 0:
        audit.record("auth.login", actor_name=form.username, outcome="deny",
                     client_ip=client_ip, detail={"reason": "locked_out",
                                                  "retry_after_sec": int(locked_for)})
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"too many failed login attempts — try again in {int(locked_for)}s",
            headers={"Retry-After": str(int(locked_for))},
        )
    user = auth.authenticate(username=form.username, password=form.password)
    if user is None:
        auth.record_login_failure(key)
        audit.record("auth.login", actor_name=form.username, outcome="deny",
                     client_ip=client_ip, detail={"reason": "bad_credentials"})
        # Deliberately identical message for "no such user" and "wrong
        # password" — anything more specific is a user-enumeration oracle.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "invalid username or password")

    # Second factor, when the account has it enabled. OAuth2PasswordRequestForm
    # has no TOTP field, so the code rides in the standard `client_secret`
    # slot — keeps the endpoint a plain OAuth2 password grant for clients.
    if user.get("mfa_enabled"):
        code = (getattr(form, "client_secret", None) or "").strip()
        if not code:
            audit.record("auth.login", actor=user, outcome="deny", client_ip=client_ip,
                         detail={"reason": "mfa_required"})
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "multi-factor code required — resend with the 6-digit code in client_secret",
                headers={"WWW-Authenticate": 'Bearer error="mfa_required"'},
            )
        secret = auth.get_totp_secret(user["id"])
        used_recovery = False
        if not (secret and mfa.verify(secret, code, user_id=user["id"])):
            # Fall back to a single-use recovery code, so losing the
            # authenticator is a self-service problem rather than one that
            # needs an admin to clear totp_secret by hand.
            used_recovery = auth.consume_recovery_code(user["id"], code)
            if not used_recovery:
                # A wrong second factor counts toward the lockout too, otherwise
                # MFA turns the code into an unthrottled guessing surface.
                auth.record_login_failure(key)
                audit.record("auth.login", actor=user, outcome="deny", client_ip=client_ip,
                             detail={"reason": "mfa_invalid"})
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid multi-factor code")
        if used_recovery:
            remaining = auth.count_recovery_codes(user["id"])["remaining"]
            audit.record("auth.mfa.recovery_used", actor=user, client_ip=client_ip,
                         detail={"remaining": remaining})

    auth.record_login_success(key)
    audit.record("auth.login", actor=user, client_ip=client_ip,
                 detail={"mfa": bool(user.get("mfa_enabled"))})
    payload = {"sub": str(user["id"]), "role": user["role"],
               "username": user["username"]}
    token = auth.create_token(payload)
    auth.record_session(auth.decode_token(token), client_ip=client_ip,
                        user_agent=request.headers.get("user-agent"))
    return TokenOut(access_token=token, user=user)


# --- Session management -----------------------------------------------------
@router.get("/auth/sessions")
async def list_sessions(request: Request,
                        include_expired: bool = False,
                        user: dict = Depends(auth.current_user)) -> dict:
    """Every session open for the calling user, so a lost device can be found
    and ended without rotating the signing secret for everyone.
    """
    sessions = auth.list_sessions(user["id"], include_expired=include_expired)
    audit.record("auth.sessions.list", actor=user, client_ip=_ip(request),
                 detail={"count": len(sessions)})
    return {"count": len(sessions), "sessions": sessions}


@router.delete("/auth/sessions/{jti}")
async def revoke_session(jti: str, request: Request,
                         user: dict = Depends(auth.current_user)) -> dict:
    ok = auth.revoke_session(jti, user_id=user["id"], reason="user_revoked")
    audit.record("auth.sessions.revoke", actor=user, resource=f"session:{jti}",
                 outcome="allow" if ok else "deny", client_ip=_ip(request))
    if not ok:
        raise HTTPException(404, "session not found")
    return {"ok": True, "revoked": jti}


@router.post("/auth/sessions/revoke-all")
async def revoke_all_sessions(request: Request,
                              keep_current: bool = True,
                              token: str = Depends(auth.oauth2_scheme),
                              user: dict = Depends(auth.current_user)) -> dict:
    current_jti = None
    if keep_current and token:
        try:
            current_jti = auth.decode_token(token).get("jti")
        except ValueError:
            current_jti = None
    n = auth.revoke_all_sessions(user["id"], except_jti=current_jti)
    audit.record("auth.sessions.revoke_all", actor=user, client_ip=_ip(request),
                 detail={"revoked": n, "kept_current": bool(current_jti)})
    return {"ok": True, "revoked": n, "kept_current": bool(current_jti)}


@router.post("/auth/logout")
async def logout(request: Request,
                 token: str = Depends(auth.oauth2_scheme)) -> dict:
    """Revoke the presented token. Without this, a leaked token stays valid
    until it expires and the only remedy is rotating the signing secret,
    which logs out every user at once.
    """
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        payload = auth.decode_token(token)
    except ValueError as e:
        # Already expired/revoked — nothing to do, and saying so is harmless.
        return {"ok": True, "detail": str(e)}
    revoked = auth.revoke_token(payload, reason="logout")
    audit.record("auth.logout", actor_id=int(payload.get("sub") or 0) or None,
                 actor_name=payload.get("username"), client_ip=_ip(request),
                 detail={"revoked": revoked})
    return {"ok": True, "revoked": revoked}


# --- Multi-factor authentication -------------------------------------------
class MfaVerifyIn(BaseModel):
    code: str = Field(min_length=4, max_length=12)


@router.post("/auth/mfa/enroll")
async def mfa_enroll(request: Request,
                     user: dict = Depends(auth.current_user)) -> dict:
    """Start enrolment: mint a secret and return it plus an otpauth:// URI.

    The secret is stored but MFA is *not* enabled until the user proves they
    can generate a code from it (``/auth/mfa/verify``), so nobody locks
    themselves out by enabling it against a secret their app never received.
    """
    secret = mfa.generate_secret()
    auth.set_totp_secret(user["id"], secret, enabled=False)
    audit.record("auth.mfa.enroll_start", actor=user, client_ip=_ip(request))
    return {
        "secret": secret,
        "otpauth_uri": mfa.provisioning_uri(secret, account=user["username"]),
        "digits": mfa.TOTP_DIGITS,
        "period": mfa.TOTP_STEP_SEC,
    }


@router.post("/auth/mfa/verify")
async def mfa_verify(request: Request, body: MfaVerifyIn,
                     user: dict = Depends(auth.current_user)) -> dict:
    """Confirm enrolment by proving a valid code, which switches MFA on."""
    secret = auth.get_totp_secret(user["id"])
    if not secret:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "no enrolment in progress — call /auth/mfa/enroll first")
    if not mfa.verify(secret, body.code, user_id=user["id"]):
        audit.record("auth.mfa.enroll_verify", actor=user, outcome="deny",
                     client_ip=_ip(request))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid code")
    auth.set_totp_secret(user["id"], secret, enabled=True)
    # Issue recovery codes at the moment MFA becomes mandatory for this
    # account — that is the only point where the user is guaranteed to be
    # looking, and after it a lost authenticator would otherwise be an
    # admin-only fix. Shown once; only hashes are stored.
    codes = auth.generate_recovery_codes(user["id"])
    audit.record("auth.mfa.enabled", actor=user, client_ip=_ip(request),
                 detail={"recovery_codes_issued": len(codes)})
    return {"ok": True, "mfa_enabled": True, "recovery_codes": codes,
            "recovery_codes_note": "Store these now — they are shown once and "
                                   "each works a single time."}


@router.post("/auth/mfa/recovery-codes")
async def regenerate_recovery_codes(request: Request, body: MfaVerifyIn,
                                    user: dict = Depends(auth.current_user)) -> dict:
    """Issue a fresh set, invalidating the old ones. Requires a current TOTP
    code so a stolen token cannot mint recovery codes for later use.
    """
    secret = auth.get_totp_secret(user["id"])
    if not secret or not mfa.verify(secret, body.code, user_id=user["id"]):
        audit.record("auth.mfa.recovery_regenerate", actor=user, outcome="deny",
                     client_ip=_ip(request))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid code")
    codes = auth.generate_recovery_codes(user["id"])
    audit.record("auth.mfa.recovery_regenerate", actor=user, client_ip=_ip(request),
                 detail={"issued": len(codes)})
    return {"ok": True, "recovery_codes": codes}


@router.get("/auth/mfa/recovery-codes")
async def recovery_code_status(user: dict = Depends(auth.current_user)) -> dict:
    """How many codes remain — the codes themselves are never retrievable."""
    return auth.count_recovery_codes(user["id"])


@router.post("/auth/mfa/disable")
async def mfa_disable(request: Request, body: MfaVerifyIn,
                      user: dict = Depends(auth.current_user)) -> dict:
    """Turn MFA off. Requires a current code — otherwise anyone holding a
    stolen token could strip the second factor off the account.
    """
    secret = auth.get_totp_secret(user["id"])
    if not secret or not mfa.verify(secret, body.code, user_id=user["id"]):
        audit.record("auth.mfa.disable", actor=user, outcome="deny", client_ip=_ip(request))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid code")
    auth.clear_totp_secret(user["id"])
    audit.record("auth.mfa.disabled", actor=user, client_ip=_ip(request))
    return {"ok": True, "mfa_enabled": False}


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
                    user: dict = Depends(rbac.require("ehr.write"))) -> dict:
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
async def ehr_list(request: Request,
                   user: dict = Depends(rbac.require("ehr.read"))) -> dict:
    records = store.list_patients(owner_user_id=user["id"])
    audit.record("ehr.list", actor=user, client_ip=_ip(request),
                 detail={"count": len(records)})
    return {"records": records}


@router.get("/ehr/{patient_id}")
async def ehr_get(patient_id: str, request: Request,
                  user: dict = Depends(rbac.require("ehr.read"))) -> dict:
    rec = store.get_patient(patient_id, owner_user_id=user["id"])
    if rec is None:
        # Audited as a denial: a miss here is either a typo or someone probing
        # for another owner's record id, and both are worth having on record.
        audit.record("ehr.read", actor=user, resource=f"patient:{patient_id}",
                     outcome="deny", client_ip=_ip(request),
                     detail={"reason": "not_found_or_not_owned"})
        raise HTTPException(404, "patient not found")
    audit.record("ehr.read", actor=user, resource=f"patient:{patient_id}",
                 client_ip=_ip(request))
    return {"patient_id": patient_id, "record": rec}


@router.delete("/ehr/{patient_id}")
async def ehr_delete(patient_id: str, request: Request,
                     user: dict = Depends(rbac.require("ehr.delete"))) -> dict:
    ok = store.delete_patient(patient_id, owner_user_id=user["id"])
    if not ok:
        audit.record("ehr.delete", actor=user, resource=f"patient:{patient_id}",
                     outcome="deny", client_ip=_ip(request),
                     detail={"reason": "not_found_or_not_owned"})
        raise HTTPException(404, "patient not found")
    audit.record("ehr.delete", actor=user, resource=f"patient:{patient_id}",
                 client_ip=_ip(request))
    return {"ok": True}


class RecordDoseIn(BaseModel):
    medication: str
    at: Optional[float] = None


@router.post("/ehr/{patient_id}/dose")
async def ehr_record_dose(patient_id: str, body: RecordDoseIn,
                          user: dict = Depends(rbac.require("ehr.write"))) -> dict:
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
                       user: dict = Depends(rbac.require("alerts.read"))) -> dict:
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
                      user: dict = Depends(rbac.require("alerts.send"))) -> dict:
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
                      user: dict = Depends(rbac.require("alerts.read"))) -> dict:
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
                  user: dict = Depends(rbac.require("education.write"))) -> dict:
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
                   user: dict = Depends(rbac.require("education.write"))) -> dict:
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
                   user: dict = Depends(rbac.require("education.write"))) -> dict:
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
                      user: dict = Depends(rbac.require("education.write"))) -> dict:
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
                        user: dict = Depends(rbac.require("knowledge.read"))) -> dict:
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
                  user: dict = Depends(rbac.require("ehr.read"))) -> dict:
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
async def rules_list(user: dict = Depends(rbac.require("knowledge.read"))) -> dict:
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
async def medrag_health(user: dict = Depends(rbac.require("models.read"))) -> dict:
    from integrations.medrag_client import MedragError, get_medrag_client

    try:
        client = get_medrag_client()
    except MedragError as e:
        return {"ok": False, "detail": str(e)}
    return await client.health()


@router.get("/education/saved")
async def edu_saved(user: dict = Depends(rbac.require("education.read"))) -> dict:
    return {"items": store.list_quizzes(owner_user_id=user["id"])}


@router.get("/education/saved/{quiz_id}")
async def edu_saved_get(quiz_id: int,
                        user: dict = Depends(rbac.require("education.read"))) -> dict:
    q = store.get_quiz(quiz_id, owner_user_id=user["id"])
    if q is None:
        raise HTTPException(404, "quiz not found")
    return q


# ===========================================================================
# Audit trail (governance)
# ===========================================================================
@router.get("/audit")
async def audit_list(
    request: Request,
    limit: int = 100,
    offset: int = 0,
    action_prefix: Optional[str] = None,
    outcome: Optional[str] = None,
    actor_id: Optional[int] = None,
    since: Optional[float] = None,
    user: dict = Depends(rbac.require("audit.read")),
) -> dict:
    """Read the audit trail. Restricted to roles granted ``audit.read``
    (admin only by default) — the trail records who looked at whose record,
    so unrestricted access to it would itself be a disclosure.

    Reading the trail is *itself* audited, which is the point: an
    investigator's access to an investigation is part of the record.
    """
    rows = audit.query(limit=limit, offset=offset, action_prefix=action_prefix,
                       outcome=outcome, actor_id=actor_id, since=since)
    total = audit.count(action_prefix=action_prefix, outcome=outcome,
                        actor_id=actor_id, since=since)
    audit.record("audit.read", actor=user, client_ip=_ip(request),
                 detail={"returned": len(rows), "filters": {
                     "action_prefix": action_prefix, "outcome": outcome,
                     "actor_id": actor_id, "since": since}})
    return {"total": total, "count": len(rows), "offset": offset, "entries": rows}


# ===========================================================================
# FHIR R4 export (interoperability)
# ===========================================================================
@router.get("/fhir/Patient/{patient_id}")
async def fhir_patient(patient_id: str, request: Request,
                       user: dict = Depends(rbac.require("ehr.read"))) -> dict:
    """The stored record as a FHIR R4 ``Patient`` resource."""
    import fhir

    rec = store.get_patient(patient_id, owner_user_id=user["id"])
    if rec is None:
        raise HTTPException(404, "patient not found")
    audit.record("fhir.read", actor=user, resource=f"patient:{patient_id}",
                 client_ip=_ip(request), detail={"resource_type": "Patient"})
    return fhir.to_patient(rec, patient_id)


@router.get("/fhir/Patient/{patient_id}/$everything")
async def fhir_everything(patient_id: str, request: Request,
                          user: dict = Depends(rbac.require("ehr.read"))) -> dict:
    """The whole record as a FHIR ``Bundle`` — Patient plus Conditions,
    AllergyIntolerances, MedicationStatements and Observations.

    Named after the standard ``$everything`` operation so FHIR clients find
    it where they expect. This is a read-only projection of AranMed's
    internal model, not a FHIR-native store.
    """
    import fhir

    rec = store.get_patient(patient_id, owner_user_id=user["id"])
    if rec is None:
        raise HTTPException(404, "patient not found")
    bundle = fhir.to_bundle(rec, patient_id)
    audit.record("fhir.read", actor=user, resource=f"patient:{patient_id}",
                 client_ip=_ip(request),
                 detail={"resource_type": "Bundle", "entries": len(bundle.get("entry", []))})
    return bundle


# ===========================================================================
# Imaging & messaging interoperability (DICOM, HL7 v2)
# ===========================================================================
@router.post("/dicom/ingest")
async def dicom_ingest(request: Request,
                       file: UploadFile = File(...),
                       render: bool = True,
                       user: dict = Depends(rbac.require("report.write"))) -> dict:
    """Read a DICOM Part 10 file: study context, patient demographics, and
    optionally a windowed PNG the vision model can actually read.

    The modality tag is a fact where the transcript is a guess, so the study
    context returned here is a stronger template-selection signal than
    dictated words — `modality_hint` is shaped to feed straight into
    /api/templates/suggest.
    """
    import base64
    import dicom_ingest

    if not dicom_ingest.available():
        raise HTTPException(503, "DICOM support unavailable (pydicom not installed)")

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty upload")
    try:
        meta = dicom_ingest.read_metadata(raw)
    except Exception as e:  # noqa: BLE001
        audit.record("dicom.ingest", actor=user, outcome="error", client_ip=_ip(request),
                     detail={"reason": str(e)[:160]})
        raise HTTPException(400, f"not a readable DICOM file: {e}") from e

    out: dict[str, Any] = {
        "study": {k: v for k, v in meta.items() if not k.startswith("patient_")},
        "patient": dicom_ingest.to_ehr_patient(meta),
    }
    if render and meta.get("has_pixel_data"):
        try:
            png = dicom_ingest.render_png(raw)
            out["image_png_base64"] = base64.b64encode(png).decode("ascii")
            out["image_bytes"] = len(png)
        except Exception as e:  # noqa: BLE001
            # Metadata is still useful without pixels; say so rather than 500.
            out["image_error"] = str(e)[:200]

    # Identifiers only — never the demographics themselves.
    audit.record("dicom.ingest", actor=user, client_ip=_ip(request),
                 resource=f"study:{meta.get('study_instance_uid', '?')}",
                 detail={"modality": meta.get("modality"),
                         "rendered": "image_png_base64" in out})
    return out


class Hl7In(BaseModel):
    message: str = Field(min_length=8)


@router.post("/hl7/parse")
async def hl7_parse(request: Request, body: Hl7In,
                    user: dict = Depends(rbac.require("ehr.read"))) -> dict:
    """Parse an inbound HL7 v2 message (ADT/ORM/ORU) into the internal shape."""
    import hl7v2

    try:
        msg = hl7v2.parse(body.message)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    ehr = hl7v2.to_ehr(msg)
    audit.record("hl7.parse", actor=user, client_ip=_ip(request),
                 detail={"message_type": msg.message_type,
                         "segments": [s[0] for s in msg.segments]})
    return {"message_type": msg.message_type, "control_id": msg.control_id,
            "segments": [s[0] for s in msg.segments], "ehr": ehr,
            "ack": hl7v2.build_ack(msg)}


class Hl7OruIn(BaseModel):
    patient_id: str
    report_text: str
    patient_name: str = ""
    accession: str = ""
    study_description: str = ""
    receiving_app: str = ""
    receiving_facility: str = ""
    final: bool = True


@router.post("/hl7/oru")
async def hl7_oru(request: Request, body: Hl7OruIn,
                  user: dict = Depends(rbac.require("report.write"))) -> dict:
    """Build an ORU^R01 carrying a finished report, for the ordering system.

    `final=false` marks the observation preliminary (OBX-11 `P`) — a draft
    must never leave here labelled final.
    """
    import hl7v2

    msg = hl7v2.build_oru(
        patient_id=body.patient_id, patient_name=body.patient_name,
        report_text=body.report_text, accession=body.accession,
        study_description=body.study_description,
        receiving_app=body.receiving_app, receiving_facility=body.receiving_facility,
        observation_status="F" if body.final else "P",
    )
    audit.record("hl7.oru", actor=user, resource=f"patient:{body.patient_id}",
                 client_ip=_ip(request),
                 detail={"final": body.final, "chars": len(body.report_text)})
    return {"message": msg, "status": "F" if body.final else "P"}
