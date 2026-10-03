"""Who is calling: a logged-in clinician or an authenticated peer facility.

Clinical endpoints (FHIR, DICOMweb, transfers, EMS) are used both by people
in this hospital's UI and by other hospitals' systems. Both arrive with a
``Bearer`` token:

* **User tokens** are the existing AranMed session tokens (``auth.py``);
  permissions come from ``rbac.json`` by role.
* **Peer tokens** are short-lived HS256 JWTs a peer facility signs with the
  secret it shares with us (looked up via the facility registry's
  ``secret_env``). Claims: ``iss`` (peer OID), ``aud`` (our OID), ``sub``
  (the practitioner acting), ``purpose`` (HL7 PurposeOfUse, e.g. TREAT,
  ETREAT, TRANSFER), ``iat``/``exp``/``jti``. Permissions come from
  ``peer_policy.json`` by the facility's ``trust_level``.

Peer tokens carry ``typ: peer+jwt`` in the header so the two kinds are
never confused. They are bearer tokens valid for ``PEER_TOKEN_TTL_SEC``
(default 300 s); with ``PEER_TOKEN_SINGLE_USE=true`` each ``jti`` is
accepted once, so a captured token cannot be replayed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, HTTPException, Request, status

import audit
import auth
import rbac
from clinicaldb import facilities, settings

PEER_TYP = "peer+jwt"
_DEFAULT_POLICY = Path(__file__).resolve().parents[1] / "data" / "peer_policy.json"
_policy_cache: Optional[dict] = None
_seen_jti: dict[str, float] = {}
_jti_lock = threading.Lock()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * ((-len(s)) % 4))


def peer_policy() -> dict:
    global _policy_cache
    if _policy_cache is None:
        path = Path(settings.env("PEER_POLICY_FILE", "") or _DEFAULT_POLICY)
        _policy_cache = json.loads(path.read_text(encoding="utf-8"))
    return _policy_cache


def peer_allows(trust_level: str, permission: str) -> bool:
    grants = peer_policy().get("levels", {}).get(trust_level or "", [])
    if "*" in grants or permission in grants:
        return True
    ns = permission.split(".", 1)[0]
    return f"{ns}.*" in grants


# ---------------------------------------------------------------------------
# Peer token mint / verify
# ---------------------------------------------------------------------------
def mint_peer_token(*, audience_oid: str, secret: str, practitioner: str = "system",
                    purpose: str = "TREAT", ttl: Optional[int] = None,
                    extra: Optional[dict] = None) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": PEER_TYP, "kid": settings.facility_oid()}
    payload = {"iss": settings.facility_oid(), "aud": audience_oid, "sub": practitioner,
               "purpose": purpose, "iat": now,
               "exp": now + (ttl or settings.env_int("PEER_TOKEN_TTL_SEC", 300)),
               "jti": secrets.token_urlsafe(16), **(extra or {})}
    h = _b64(json.dumps(header, separators=(",", ":")).encode())
    p = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"


def token_for_peer(facility: dict, *, practitioner: str = "system",
                   purpose: str = "TREAT") -> str:
    secret = facilities.peer_secret(facility)
    if not secret:
        raise PermissionError(f"no shared secret configured for {facility.get('oid')} "
                              f"(set {facility.get('secret_env') or 'secret_env'})")
    return mint_peer_token(audience_oid=facility["oid"], secret=secret,
                           practitioner=practitioner, purpose=purpose)


def is_peer_token(token: str) -> bool:
    try:
        header = json.loads(_b64d(token.split(".")[0]))
    except Exception:  # noqa: BLE001
        return False
    return header.get("typ") == PEER_TYP


def verify_peer_token(token: str) -> dict:
    """Return ``{facility, claims}`` or raise ValueError."""
    try:
        h, p, sig = token.split(".")
        claims = json.loads(_b64d(p))
    except Exception as e:  # noqa: BLE001
        raise ValueError("malformed peer token") from e
    iss = claims.get("iss")
    facility = facilities.get_by_oid(iss) if iss else None
    if not facility or facility.get("is_local") or not facility.get("active"):
        raise ValueError("unknown or inactive peer facility")
    secret = facilities.peer_secret(facility)
    if not secret:
        raise ValueError("no shared secret configured for this peer")
    expected = _b64(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        raise ValueError("bad peer token signature")
    now = time.time()
    if claims.get("exp", 0) < now:
        raise ValueError("peer token expired")
    if claims.get("iat", 0) > now + 60:
        raise ValueError("peer token issued in the future")
    if claims.get("aud") != settings.facility_oid():
        raise ValueError("peer token audience is not this facility")
    if claims.get("purpose") not in peer_policy().get("purposes", []):
        raise ValueError("purpose of use not accepted")
    jti = claims.get("jti")
    if jti:
        with _jti_lock:
            for k, exp in list(_seen_jti.items()):
                if exp < now:
                    _seen_jti.pop(k, None)
            if jti in _seen_jti and settings.env_bool("PEER_TOKEN_SINGLE_USE", False):
                raise ValueError("peer token replayed")
            _seen_jti[jti] = float(claims.get("exp", now))
    return {"facility": facility, "claims": claims}


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------
def _bearer(request: Request) -> Optional[str]:
    h = request.headers.get("authorization") or ""
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return request.query_params.get("access_token") if settings.env_bool(
        "ALLOW_QUERY_TOKEN", False) else None


def resolve_principal(request: Request) -> dict:
    token = _bearer(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    if is_peer_token(token):
        try:
            v = verify_peer_token(token)
        except ValueError as e:
            audit.record("peer.auth", actor_name="peer:?", outcome="deny",
                         client_ip=request.client.host if request.client else None,
                         detail={"reason": str(e), "path": request.url.path})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e
        f, c = v["facility"], v["claims"]
        return {"id": None, "username": f"peer:{f['oid']}:{c.get('sub')}", "role": "peer",
                "kind": "peer", "facility": f, "facility_oid": f["oid"],
                "practitioner": c.get("sub"), "purpose": c.get("purpose")}
    try:
        payload = auth.decode_token(token)
    except ValueError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e),
                            headers={"WWW-Authenticate": "Bearer"}) from e
    user = auth.get_user(int(payload["sub"]))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found")
    return {**user, "kind": "user", "facility_oid": settings.facility_oid()}


def allows(principal: dict, permission: str) -> bool:
    if principal.get("kind") == "peer":
        return peer_allows(principal["facility"].get("trust_level") or "", permission)
    return rbac.allows((principal.get("role") or "").lower(), permission)


def require(permission: str):
    """Dependency: a user *or* peer principal holding *permission*."""
    async def _dep(request: Request) -> dict:
        p = resolve_principal(request)
        if allows(p, permission):
            return p
        audit.record("rbac.deny", actor_name=p.get("username"), actor_id=p.get("id"),
                     resource=permission, outcome="deny",
                     client_ip=request.client.host if request.client else None,
                     detail={"kind": p.get("kind"), "path": request.url.path})
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"not permitted to {permission}")
    return _dep


def actor(principal: dict) -> dict[str, Any]:
    """kwargs for audit.record identifying the principal."""
    return {"actor_id": principal.get("id"), "actor_name": principal.get("username")}
