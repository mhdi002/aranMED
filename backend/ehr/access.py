"""Who may see a patient's record: consent, restriction and break-the-glass.

Two questions, answered separately:

* **Local clinicians.** Everyone with ``clinical.read`` may read local
  records, except a *restricted* record (an active Consent with category
  ``restricted`` — VIP, staff member, sensitive diagnosis). Opening one
  requires break-the-glass: a stated reason, ``clinical.breakglass``
  permission, a time-limited grant, and an audit row that names it.
* **Peer facilities.** Sharing follows ``CONSENT_SHARING_DEFAULT``:
  ``opt-out`` (default — shared for treatment unless the patient has an
  active ``deny-sharing`` consent covering that facility or ``*``) or
  ``opt-in`` (shared only with an active ``permit-sharing`` consent). A
  peer declaring purpose ``ETREAT`` (emergency treatment) overrides a
  denial — the cross-hospital break-the-glass — and is audited as such.
"""
from __future__ import annotations

import time
from typing import Optional

import audit
import db
from clinicaldb import settings
from clinicaldb.util import new_id, row
from ehr import store


def _active(c: dict) -> bool:
    if c.get("status") != "active":
        return False
    today = time.strftime("%Y-%m-%d")
    if c.get("start_at") and c["start_at"][:10] > today:
        return False
    if c.get("end_at") and c["end_at"][:10] < today:
        return False
    return True


def consents(person_id: str) -> list[dict]:
    return [c for c in store.list_for("consent", person_id) if _active(c)]


def is_restricted(person_id: str) -> bool:
    return any(c.get("category") == "restricted" for c in consents(person_id))


def active_breakglass(person_id: str, user_id: Optional[int]) -> Optional[dict]:
    if user_id is None:
        return None
    with db.connect() as c:
        r = c.execute("SELECT * FROM ehr_breakglass WHERE person_id=? AND user_id=? "
                      "AND expires_at > ? ORDER BY expires_at DESC LIMIT 1",
                      (person_id, user_id, db.now())).fetchone()
    return row(r)


def break_glass(person_id: str, user: dict, reason: str, *,
                minutes: Optional[int] = None) -> dict:
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise ValueError("a specific reason (at least 10 characters) is required")
    minutes = minutes or settings.env_int("BREAKGLASS_MINUTES", 240)
    g = {"id": new_id(), "person_id": person_id, "user_id": user.get("id"),
         "username": user.get("username"), "reason": reason,
         "expires_at": db.now() + minutes * 60, "created_at": db.now()}
    with db.connect() as c:
        c.execute("INSERT INTO ehr_breakglass(id, person_id, user_id, username, reason, "
                  "expires_at, created_at) VALUES (?,?,?,?,?,?,?)", tuple(g.values()))
    audit.record("clinical.breakglass", actor=user, resource=f"person:{person_id}",
                 outcome="allow", detail={"reason": reason, "minutes": minutes})
    return g


def local_read_decision(person_id: str, principal: dict) -> tuple[bool, str]:
    """(allowed, why) for a local user principal."""
    if not is_restricted(person_id):
        return True, "unrestricted"
    if active_breakglass(person_id, principal.get("id")):
        return True, "break-glass"
    return False, "restricted record: break-the-glass required"


def peer_read_decision(person_id: str, facility_oid: str, purpose: str) -> tuple[bool, str]:
    mode = settings.env("CONSENT_SHARING_DEFAULT", "opt-out").lower()
    cs = consents(person_id)

    def covers(c: dict) -> bool:
        g = (c.get("grantee") or "*").strip()
        return g in ("*", facility_oid)
    denied = any(c.get("category") == "deny-sharing" and covers(c) for c in cs)
    permitted = any(c.get("category") == "permit-sharing" and covers(c) for c in cs)
    if purpose == "ETREAT":
        return True, "emergency override" if (denied or (mode == "opt-in" and not permitted)) \
            else "emergency treatment"
    if denied:
        return False, "patient has refused sharing with this facility"
    if mode == "opt-in" and not permitted:
        return False, "no sharing consent on file (opt-in policy)"
    return True, "consented" if permitted else "opt-out policy"


def decide(person_id: str, principal: dict) -> tuple[bool, str]:
    if principal.get("kind") == "peer":
        return peer_read_decision(person_id, principal["facility_oid"],
                                  principal.get("purpose") or "TREAT")
    return local_read_decision(person_id, principal)
