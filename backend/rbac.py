"""Role-Based Access Control.

Roles were stored on the user row from the start, but nothing ever checked
them: any authenticated account could call every route, including deleting
another clinician's patient record. This module makes the role mean something.

The policy is **data, not code** — `backend/data/rbac.json`, overridable with
``RBAC_POLICY_FILE`` — so changing who may do what is an edit and a restart,
not a code change. Grants support exact permissions (``ehr.read``), namespace
wildcards (``ehr.*``), and a full wildcard (``*``).

Deny by default: an unknown role, a missing policy file, or an unlisted
permission all result in denial. A permissions system that fails open is
worse than none, because it invites the assumption it is working.

Ownership scoping is separate and still applies underneath: RBAC decides
*whether* a caller may touch EHR at all, ``owner_user_id`` decides *which*
records. Both must pass.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, Request, status

import audit
import auth
import config

log = logging.getLogger("rbac")

_policy: Optional[dict] = None
_lock = threading.Lock()


def policy_path() -> Path:
    override = os.environ.get("RBAC_POLICY_FILE", "").strip()
    if override:
        return Path(override)
    return config.ROOT / "data" / "rbac.json"


def load_policy(force: bool = False) -> dict:
    global _policy
    with _lock:
        if _policy is not None and not force:
            return _policy
        path = policy_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            roles = doc.get("roles") or {}
            if not roles:
                raise ValueError("policy has no 'roles'")
            _policy = {r: list(g or []) for r, g in roles.items()}
            log.info("rbac: loaded policy from %s (%d roles)", path, len(_policy))
        except (OSError, ValueError) as e:
            # Deny-by-default: an empty policy denies everything except the
            # break-glass admin below, which is safer than guessing grants.
            log.error("rbac: could not load policy from %s (%s) — denying all "
                      "non-admin access until it is fixed", path, e)
            _policy = {}
        return _policy


def reload_policy() -> dict:
    return load_policy(force=True)


def grants_for(role: str) -> list[str]:
    pol = load_policy()
    if role in pol:
        return pol[role]
    # Break-glass: 'admin' keeps full access even if the policy file is
    # unreadable, so a bad edit can't lock every operator out of the system.
    if role == "admin":
        return ["*"]
    return []


def allows(role: str, permission: str) -> bool:
    """True when *role* grants *permission*.

    Matching: exact, namespace wildcard (``ehr.*`` covers ``ehr.read``), or
    the global ``*``.
    """
    if not permission:
        return False
    granted = grants_for(role)
    if "*" in granted:
        return True
    if permission in granted:
        return True
    namespace = permission.split(".", 1)[0]
    return f"{namespace}.*" in granted


def require(permission: str):
    """FastAPI dependency factory enforcing *permission*.

    Every denial is written to the audit trail — a denied attempt is exactly
    the event an auditor cares about, and silently 403ing loses it.

    Usage::

        @router.delete("/ehr/{patient_id}")
        async def delete(patient_id: str,
                         user: dict = Depends(rbac.require("ehr.delete"))):
            ...

    Returns the authenticated user dict, so it replaces
    ``Depends(auth.current_user)`` rather than sitting alongside it.
    """
    async def _dep(request: Request,
                   user: dict = Depends(auth.current_user)) -> dict:
        role = (user.get("role") or "").strip().lower()
        if allows(role, permission):
            return user
        client_ip = request.client.host if request.client else None
        audit.record(
            "rbac.deny",
            actor=user,
            resource=permission,
            outcome="deny",
            client_ip=client_ip,
            detail={"role": role, "permission": permission,
                    "path": request.url.path, "method": request.method},
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"role '{role}' is not permitted to {permission}",
        )
    return _dep
