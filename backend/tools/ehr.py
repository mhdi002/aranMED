"""EHR (Electronic Health Record) optimiser & builder tools.

These tools let the core LLM turn raw, free-form patient information
(possibly bilingual Persian + English) into a clean, structured EHR record,
persist it, and read it back. The same record then feeds the
medication-alert tools in :mod:`tools.alerts`.

Storage: the SQLite ``patients`` table (``backend/store.py`` /
``backend/db.py``) — the same canonical store the ``/api/ehr/*`` REST
routes use. This used to be a separate one-file-per-patient JSON store at
``backend/data/ehr/<patient_id>.json`` with no owner concept; see
docs/core/CLINICAL_DATA_FABRIC_v1.md for why/how it was folded in.
``POST /api/chat`` (the only entry point that reaches these tools) carries
no authenticated user, so records built here are attributed to a
configured system-owner account (``config.EHR_TOOL_SYSTEM_OWNER_USERNAME``)
rather than a per-request user — the same effective namespace the old
ownerless file store had.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

import config
import db
import store
from auth import ensure_default_admin

from .base import Tool, ToolContext, ToolResult, tool

log = logging.getLogger("tools.ehr")


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return base or uuid.uuid4().hex[:8]


def _system_owner_user_id() -> int:
    """Resolve config.EHR_TOOL_SYSTEM_OWNER_USERNAME to a users.id.

    Seeds the default admin (idempotent, no-op if any user already exists)
    so this works standalone in unit tests too, not only behind a full app
    startup. See docs/core/CLINICAL_DATA_FABRIC_v1.md.
    """
    username = config.EHR_TOOL_SYSTEM_OWNER_USERNAME.strip().lower()
    with db.connect() as c:
        row = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if row is not None:
        return row["id"]
    ensure_default_admin()
    with db.connect() as c:
        row = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if row is None:
        raise RuntimeError(
            f"EHR system-owner user '{username}' not found — set "
            "EHR_TOOL_SYSTEM_OWNER_USERNAME to an existing username"
        )
    return row["id"]


# --------------------------------------------------------------------------
# Storage helpers — thin wrappers over backend/store.py (see module docstring)
# --------------------------------------------------------------------------
def load_record(patient_id: str) -> dict | None:
    return store.get_patient(patient_id, owner_user_id=_system_owner_user_id())


def save_record(record: dict) -> dict:
    pid = record["id"]
    language = record.get("language", "en")
    data = {k: v for k, v in record.items() if k not in ("id", "language")}
    return store.upsert_patient(
        owner_user_id=_system_owner_user_id(),
        data=data,
        patient_id=pid,
        language=language,
    )


def list_records() -> list[dict]:
    return store.list_patients(owner_user_id=_system_owner_user_id())


# --------------------------------------------------------------------------
# LLM prompt
# --------------------------------------------------------------------------
_EHR_SYS_EN = """You are a clinical informatics assistant that converts raw
patient information into a STRICT JSON Electronic Health Record.

Rules:
- Output ONLY valid JSON. No markdown, no commentary, no code fences.
- Use this exact schema (fill what is present, use null / [] when unknown):
{
  "patient":   {"name": str|null, "age": number|null, "sex": str|null,
                "mrn": str|null, "weight_kg": number|null},
  "encounter": {"date": str|null, "chief_complaint": str|null,
                "summary": str|null},
  "problems":  [ {"name": str, "status": "active"|"resolved"|null} ],
  "allergies": [ {"substance": str, "reaction": str|null} ],
  "medications": [
     {"name": str, "dose": str|null, "route": str|null,
      "frequency": str|null, "frequency_hours": number|null,
      "indication": str|null, "notes": str|null}
  ],
  "vitals":   {"bp": str|null, "hr": number|null, "temp_c": number|null,
               "spo2": number|null, "rr": number|null},
  "notes": str|null
}
- "frequency_hours" is the numeric dosing interval in hours when it can be
  inferred (e.g. "every 8 hours" -> 8, "BID" -> 12, "once daily" -> 24).
- Translate Persian content into clinical English for the field VALUES, but
  keep medication brand names as written.
"""

_EHR_SYS_FA = """You are a clinical informatics assistant. Convert raw patient
information into a STRICT JSON Electronic Health Record whose field VALUES are
written in Persian (فارسی), except medication brand names which stay as given.

Rules:
- Output ONLY valid JSON. No markdown, no commentary, no code fences.
- Use exactly this schema (use null / [] when unknown):
{
  "patient":   {"name": str|null, "age": number|null, "sex": str|null,
                "mrn": str|null, "weight_kg": number|null},
  "encounter": {"date": str|null, "chief_complaint": str|null,
                "summary": str|null},
  "problems":  [ {"name": str, "status": "active"|"resolved"|null} ],
  "allergies": [ {"substance": str, "reaction": str|null} ],
  "medications": [
     {"name": str, "dose": str|null, "route": str|null,
      "frequency": str|null, "frequency_hours": number|null,
      "indication": str|null, "notes": str|null}
  ],
  "vitals":   {"bp": str|null, "hr": number|null, "temp_c": number|null,
               "spo2": number|null, "rr": number|null},
  "notes": str|null
}
- "frequency_hours" عددی است: فاصله‌ی دوزها بر حسب ساعت (مثلاً «هر ۸ ساعت» → 8).
"""


def _extract_json(text: str) -> dict:
    """Best-effort: pull the first {...} block and parse it."""
    text = text.strip()
    # strip code fences if the model added them anyway
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in model output")
    return json.loads(text[start:end + 1])


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
@tool
class BuildEHRTool(Tool):
    name = "build_ehr"
    description = (
        "Convert free-form patient information (Persian and/or English) into a "
        "structured Electronic Health Record (EHR) and persist it. Returns the "
        "patient id and the structured record. Use when the user provides "
        "patient details, a clinic note, or a hand-off summary."
    )
    parameters = {
        "type": "object",
        "properties": {
            "patient_info": {
                "type": "string",
                "description": "Raw patient information / clinical note.",
            },
            "language": {
                "type": "string",
                "enum": ["en", "fa"],
                "description": "Output language for the EHR field values.",
            },
            "patient_id": {
                "type": "string",
                "description": "Optional id to update an existing record.",
            },
        },
        "required": ["patient_info"],
    }

    async def run(self, ctx: ToolContext, patient_info: str,
                  language: str = "en",
                  patient_id: str | None = None) -> ToolResult:
        from providers.base import ChatMessage

        if not patient_info.strip():
            return ToolResult(content="", error="patient_info is empty")

        sys = _EHR_SYS_FA if language == "fa" else _EHR_SYS_EN
        core = await ctx.registry.get_text("core")
        out = await core.chat(
            [ChatMessage(role="system", content=sys),
             ChatMessage(role="user", content=patient_info)],
            temperature=0.1, max_tokens=1200,
        )
        try:
            data = _extract_json(out.content)
        except Exception as e:  # noqa: BLE001
            return ToolResult(content="", error=f"could not parse EHR JSON: {e}",
                              data={"raw": out.content})

        name = (data.get("patient") or {}).get("name") or "patient"
        pid = patient_id or load_existing_id(name) or _slugify(name)
        record = {
            "id": pid,
            "language": language,
            "created_at": (load_record(pid) or {}).get("created_at", time.time()),
            **data,
        }
        save_record(record)
        ctx.state["ehr_patient_id"] = pid
        ctx.state["ehr_record"] = record

        meds = record.get("medications", [])
        return ToolResult(
            content=(f"EHR saved for '{name}' (id={pid}) with "
                     f"{len(meds)} medication(s)."),
            data={"patient_id": pid, "record": record},
        )


@tool
class GetEHRTool(Tool):
    name = "get_ehr"
    description = "Fetch a previously stored EHR record by patient id."
    parameters = {
        "type": "object",
        "properties": {
            "patient_id": {"type": "string"},
        },
        "required": ["patient_id"],
    }

    async def run(self, ctx: ToolContext, patient_id: str) -> ToolResult:
        rec = load_record(patient_id)
        if rec is None:
            return ToolResult(content="", error=f"no EHR for id {patient_id}")
        ctx.state["ehr_patient_id"] = patient_id
        ctx.state["ehr_record"] = rec
        return ToolResult(
            content=f"Loaded EHR for {patient_id}.",
            data={"patient_id": patient_id, "record": rec},
        )


@tool
class ListEHRTool(Tool):
    name = "list_ehr"
    description = "List all stored EHR records (id, name, medication count)."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, ctx: ToolContext) -> ToolResult:
        items = list_records()
        return ToolResult(
            content=f"{len(items)} EHR record(s) stored.",
            data={"records": items},
        )


def load_existing_id(name: str) -> str | None:
    """Return an existing record id whose patient name matches *name*."""
    target = _slugify(name)
    for item in list_records():
        if item.get("id") == target:
            return target
    return None
