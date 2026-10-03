"""Clinical (EHR / interop) tools for the agent.

Same rules as the clinician UI: a signed-in user, the role's permission,
the record's access decision (restricted records need break-the-glass,
which the agent cannot perform for the user), and an audit row per call.
Remote records from peer hospitals are fetched under the user's identity
and marked with their source facility in every answer.
"""
from __future__ import annotations

from typing import Any, Optional

import audit

from .base import Tool, ToolContext, ToolResult, tool
from .pacs import _user


def _person(person_id: str):
    from clinicaldb import mpi
    return mpi.get(person_id)


def _guard(person_id: str, user: dict) -> Optional[str]:
    from ehr import access
    ok, why = access.decide(person_id, {**user, "kind": "user"})
    audit.record("clinical.agent.read", actor=user, resource=f"person:{person_id}",
                 outcome="allow" if ok else "deny", detail={"why": why, "via": "agent"})
    return None if ok else why


@tool
class ClinicalPatientSearch(Tool):
    name = "clinical_patient_search"
    description = ("Find patients in the master patient index by name, identifier (MRN, national "
                   "id) or birth date (YYYY-MM-DD). scope='network' also searches peer hospitals. "
                   "Returns person ids for the other clinical_* tools.")
    parameters = {"type": "object", "properties": {
        "query": {"type": "string", "description": "name or identifier"},
        "birth_date": {"type": "string"},
        "scope": {"type": "string", "enum": ["local", "network"]}}, "required": []}

    async def run(self, ctx: ToolContext, query: Optional[str] = None,
                  birth_date: Optional[str] = None, scope: str = "local") -> ToolResult:
        user, err = _user(ctx, "clinical.read")
        if err:
            return ToolResult(content="", error=err)
        if not (query or birth_date):
            return ToolResult(content="", error="give a name/identifier or birth date")
        from starlette.concurrency import run_in_threadpool
        from clinicaldb import mpi
        ident = query if query and any(ch.isdigit() for ch in query) else None
        hits = await run_in_threadpool(mpi.search, name=None if ident else query,
                                       identifier=ident, birth_date=birth_date)
        remote: list[dict] = []
        errors: list[dict] = []
        if scope == "network":
            from interop import federation
            res = await run_in_threadpool(federation.search_patients, q=query, identifier=ident,
                                          birth_date=birth_date, principal=user)
            remote, errors = res["results"], res["errors"]
        audit.record("clinical.search", actor=user, detail={"via": "agent", "local": len(hits),
                                                            "remote": len(remote)})
        lines = [f"- {h['name']} DOB {h['demographics'].get('birth_date') or '?'} "
                 f"sex {h['demographics'].get('sex') or '?'} person_id={h['person_id']} "
                 f"ids: {', '.join(i['value'] for i in h['identifiers'][:4])}" for h in hits]
        lines += [f"- [at {r['facility']}] {r['name']} DOB {r['demographics'].get('birth_date') or '?'}"
                  for r in remote]
        text = ("Patients:\n" + "\n".join(lines)) if lines else "No matching patients."
        if errors:
            text += "\n(unreachable: " + ", ".join(e["facility"] for e in errors) + ")"
        return ToolResult(content=text, data={"local": hits, "remote": remote, "errors": errors})


@tool
class ClinicalPatientSummary(Tool):
    name = "clinical_patient_summary"
    description = ("Unified clinical summary of a patient: problems, allergies, medications, "
                   "vitals, abnormal labs, encounters, imaging and reports — including records "
                   "held at other hospitals when include_remote is true (each item names its "
                   "source hospital).")
    parameters = {"type": "object", "properties": {
        "person_id": {"type": "string"}, "include_remote": {"type": "boolean"}},
        "required": ["person_id"]}

    async def run(self, ctx: ToolContext, person_id: str, include_remote: bool = True) -> ToolResult:
        user, err = _user(ctx, "clinical.read")
        if err:
            return ToolResult(content="", error=err)
        p = _person(person_id)
        if not p:
            return ToolResult(content="", error="unknown person")
        denied = _guard(p["id"], user)
        if denied:
            return ToolResult(content="", error=denied)
        from starlette.concurrency import run_in_threadpool
        from ehr import chart
        c = await run_in_threadpool(chart.build, p["id"], principal={**user, "kind": "user"},
                                    include_remote=include_remote)
        text = chart.as_text(c)
        if c["errors"]:
            text += "\nNot available from: " + "; ".join(
                f"{e.get('facility') or e.get('source')}: {e.get('error')}" for e in c["errors"])
        ctx.state["person_id"] = p["id"]
        return ToolResult(content=text, data={"summary": c["summary"], "facilities": c["facilities"],
                                              "errors": c["errors"]})


@tool
class ClinicalTimeline(Tool):
    name = "clinical_timeline"
    description = "Chronological timeline of a patient's encounters, results, imaging and documents."
    parameters = {"type": "object", "properties": {
        "person_id": {"type": "string"}, "include_remote": {"type": "boolean"},
        "limit": {"type": "integer"}}, "required": ["person_id"]}

    async def run(self, ctx: ToolContext, person_id: str, include_remote: bool = False,
                  limit: int = 25) -> ToolResult:
        user, err = _user(ctx, "clinical.read")
        if err:
            return ToolResult(content="", error=err)
        p = _person(person_id)
        if not p:
            return ToolResult(content="", error="unknown person")
        denied = _guard(p["id"], user)
        if denied:
            return ToolResult(content="", error=denied)
        from starlette.concurrency import run_in_threadpool
        from ehr import chart
        c = await run_in_threadpool(chart.build, p["id"], principal={**user, "kind": "user"},
                                    include_remote=include_remote)
        events = chart.timeline(c)[: max(1, min(limit, 200))]
        lines = [f"- {e['date'] or '?'} [{e['kind']}] {e['label']}"
                 + (f" ({e['source']['facility']})" if e.get("source") and e["source"].get("remote") else "")
                 for e in events]
        return ToolResult(content="Timeline:\n" + "\n".join(lines) if lines else "No events.",
                          data={"events": events})


@tool
class RequestPatientTransfer(Tool):
    name = "request_patient_transfer"
    description = ("Request an inter-hospital transfer of a patient to a peer facility (by OID). "
                   "The receiving hospital must accept; the record and imaging are then sent.")
    parameters = {"type": "object", "properties": {
        "person_id": {"type": "string"}, "to_facility": {"type": "string"},
        "reason": {"type": "string"}, "urgency": {"type": "string", "enum": ["routine", "urgent", "emergent"]},
        "clinical_summary": {"type": "string"}}, "required": ["person_id", "to_facility", "reason"]}

    async def run(self, ctx: ToolContext, person_id: str, to_facility: str, reason: str,
                  urgency: str = "routine", clinical_summary: str = "") -> ToolResult:
        user, err = _user(ctx, "transfer.write")
        if err:
            return ToolResult(content="", error=err)
        from starlette.concurrency import run_in_threadpool
        from interop import transfers
        try:
            t = await run_in_threadpool(transfers.request, person_id, to_facility, user=user,
                                        urgency=urgency, reason=reason,
                                        clinical_summary=clinical_summary)
        except (transfers.TransferError, PermissionError) as e:
            return ToolResult(content="", error=str(e))
        return ToolResult(content=f"Transfer requested to {to_facility} (status {t['status']}, id {t['id']}). "
                                  "Waiting for the receiving hospital to accept.", data={"transfer": t})


@tool
class TransferStatus(Tool):
    name = "transfer_status"
    description = "Status of inter-hospital transfers, for one patient or all recent ones."
    parameters = {"type": "object", "properties": {"person_id": {"type": "string"}}, "required": []}

    async def run(self, ctx: ToolContext, person_id: Optional[str] = None) -> ToolResult:
        user, err = _user(ctx, "transfer.read")
        if err:
            return ToolResult(content="", error=err)
        from interop import transfers
        items = [t for t in transfers.list_all(limit=50) if not person_id or t["person_id"] == person_id]
        lines = [f"- {t['direction']} {t['from_facility_name']} → {t['to_facility_name']}: {t['status']} "
                 f"(package {t.get('package_status') or '-'}) patient {((t.get('patient') or {}).get('name')) or '?'}"
                 for t in items]
        return ToolResult(content="Transfers:\n" + "\n".join(lines) if lines else "No transfers.",
                          data={"transfers": items})


@tool
class EmsInbound(Tool):
    name = "ems_inbound"
    description = "Ambulances inbound to this hospital (EMS pre-arrival board) with MIST handoff summaries."
    parameters = {"type": "object", "properties": {"status": {"type": "string"}}, "required": []}

    async def run(self, ctx: ToolContext, status: Optional[str] = None) -> ToolResult:
        user, err = _user(ctx, "ems.read")
        if err:
            return ToolResult(content="", error=err)
        from interop import ems
        items = ems.board(status=status or None)
        lines = [f"- [{n['status']}] {n.get('triage') or ''} unit {n.get('unit') or '?'} ETA {n.get('eta') or '?'} "
                 f"patient {((n.get('patient') or {}).get('name')) or '?'}\n  " + (n.get("summary") or "").replace("\n", "\n  ")
                 for n in items]
        audit.record("ems.board.read", actor=user, detail={"via": "agent", "count": len(items)})
        return ToolResult(content="EMS inbound:\n" + "\n".join(lines) if lines else "No inbound ambulances.",
                          data={"notifications": items})
