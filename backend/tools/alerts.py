"""Autonomous patient-state alerting tools.

Given a stored EHR record (see :mod:`tools.ehr`), these tools:

* compute which medication doses are **due** (based on each medication's
  ``frequency_hours`` and the time of the last recorded dose),
* draft a concise clinician-facing report (English or Persian),
* deliver that report to the responsible doctor via **email** (SMTP) or
  **SMS** (Twilio if configured, otherwise a logged dry-run).

Delivery is configured purely through environment variables so no secrets
live in the repo:

    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_TLS=1
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM

If the relevant variables are missing the tools still succeed but run in
``dry_run`` mode, returning the exact message that *would* have been sent.
"""
from __future__ import annotations

import logging
import os
import smtplib
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any

from .base import Tool, ToolContext, ToolResult, tool
from .ehr import load_record, save_record

log = logging.getLogger("tools.alerts")


# --------------------------------------------------------------------------
# Dosing logic
# --------------------------------------------------------------------------
def _now() -> float:
    return time.time()


def compute_due_medications(record: dict, *, now: float | None = None) -> list[dict]:
    """Return a list of medications, each annotated with dosing status.

    A medication is considered *due* when
    ``now >= last_dose_at + frequency_hours*3600``.  Medications without a
    ``frequency_hours`` are reported as ``schedule_unknown``.
    """
    now = now if now is not None else _now()
    results: list[dict] = []
    for med in record.get("medications", []):
        freq_h = med.get("frequency_hours")
        last = med.get("last_dose_at")
        entry = {
            "name": med.get("name"),
            "dose": med.get("dose"),
            "route": med.get("route"),
            "frequency": med.get("frequency"),
            "frequency_hours": freq_h,
            "indication": med.get("indication"),
            "last_dose_at": last,
        }
        if not freq_h:
            entry["status"] = "schedule_unknown"
            entry["due"] = False
        elif last is None:
            entry["status"] = "never_administered"
            entry["due"] = True
            entry["overdue_hours"] = None
        else:
            next_due = last + float(freq_h) * 3600.0
            overdue_h = (now - next_due) / 3600.0
            if now >= next_due:
                entry["status"] = "due"
                entry["due"] = True
                entry["overdue_hours"] = round(overdue_h, 2)
            else:
                entry["status"] = "scheduled"
                entry["due"] = False
                entry["hours_until_due"] = round(-overdue_h, 2)
            entry["next_due_at"] = next_due
        results.append(entry)
    return results


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def build_alert_text(record: dict, due: list[dict], *, language: str = "en") -> str:
    name = (record.get("patient") or {}).get("name") or record.get("id")
    due_only = [d for d in due if d.get("due")]
    if language == "fa":
        lines = [f"گزارش وضعیت دارویی بیمار: {name}", ""]
        if not due_only:
            lines.append("در حال حاضر هیچ داروی سررسید شده‌ای وجود ندارد.")
        else:
            lines.append("داروهای سررسید شده:")
            for d in due_only:
                od = d.get("overdue_hours")
                od_txt = f" (با {od} ساعت تأخیر)" if od else ""
                lines.append(
                    f"• {d['name']} — دوز {d.get('dose') or '—'}،"
                    f" هر {d.get('frequency_hours')} ساعت{od_txt}"
                )
        return "\n".join(lines)

    lines = [f"Medication status report — patient: {name}", ""]
    if not due_only:
        lines.append("No medications are currently due.")
    else:
        lines.append("Doses due now:")
        for d in due_only:
            od = d.get("overdue_hours")
            od_txt = f" (overdue {od} h)" if od else ""
            lines.append(
                f"• {d['name']} — dose {d.get('dose') or '—'},"
                f" every {d.get('frequency_hours')} h,"
                f" last given {_fmt_ts(d.get('last_dose_at'))}{od_txt}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Delivery back-ends
# --------------------------------------------------------------------------
def send_email(*, to: str, subject: str, body: str) -> dict:
    host = os.environ.get("SMTP_HOST")
    if not host:
        log.info("SMTP not configured — dry-run email to %s", to)
        return {"channel": "email", "to": to, "dry_run": True,
                "subject": subject, "body": body}
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("SMTP_FROM", user or "noreply@asr-agent.local")
    use_tls = os.environ.get("SMTP_TLS", "1") not in ("0", "false", "False")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    with smtplib.SMTP(host, port, timeout=20) as s:
        if use_tls:
            s.starttls()
        if user and pw:
            s.login(user, pw)
        s.sendmail(sender, [to], msg.as_string())
    return {"channel": "email", "to": to, "dry_run": False, "subject": subject}


def send_sms(*, to: str, body: str) -> dict:
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    frm = os.environ.get("TWILIO_FROM")
    if not (sid and token and frm):
        log.info("Twilio not configured — dry-run SMS to %s", to)
        return {"channel": "sms", "to": to, "dry_run": True, "body": body}
    try:
        import httpx
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        r = httpx.post(url, data={"To": to, "From": frm, "Body": body},
                       auth=(sid, token), timeout=20)
        r.raise_for_status()
        return {"channel": "sms", "to": to, "dry_run": False,
                "sid": r.json().get("sid")}
    except Exception as e:  # noqa: BLE001
        return {"channel": "sms", "to": to, "dry_run": True,
                "error": str(e), "body": body}


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
@tool
class CheckMedicationsTool(Tool):
    name = "check_medications"
    description = (
        "Given a patient id, compute which medication doses are currently due "
        "or overdue based on the stored EHR. Returns a per-drug schedule and a "
        "ready-to-send summary."
    )
    parameters = {
        "type": "object",
        "properties": {
            "patient_id": {"type": "string"},
            "language": {"type": "string", "enum": ["en", "fa"]},
        },
        "required": ["patient_id"],
    }

    async def run(self, ctx: ToolContext, patient_id: str,
                  language: str = "en") -> ToolResult:
        record = load_record(patient_id, ctx)
        if record is None:
            return ToolResult(content="", error=f"no EHR for id {patient_id}")
        due = compute_due_medications(record)
        summary = build_alert_text(record, due, language=language)
        ctx.state["due_medications"] = due
        return ToolResult(
            content=summary,
            data={"patient_id": patient_id, "medications": due,
                  "summary": summary,
                  "due_count": sum(1 for d in due if d.get("due"))},
        )


@tool
class RecordDoseTool(Tool):
    name = "record_dose"
    description = (
        "Mark that a medication dose was just administered to a patient so the "
        "alerting schedule resets. Updates the stored EHR."
    )
    parameters = {
        "type": "object",
        "properties": {
            "patient_id": {"type": "string"},
            "medication": {"type": "string",
                           "description": "Medication name to mark as given."},
            "at": {"type": "number",
                   "description": "Optional unix timestamp; defaults to now."},
        },
        "required": ["patient_id", "medication"],
    }

    async def run(self, ctx: ToolContext, patient_id: str, medication: str,
                  at: float | None = None) -> ToolResult:
        record = load_record(patient_id, ctx)
        if record is None:
            return ToolResult(content="", error=f"no EHR for id {patient_id}")
        ts = at if at is not None else _now()
        hit = None
        for med in record.get("medications", []):
            if (med.get("name") or "").lower() == medication.lower():
                med["last_dose_at"] = ts
                hit = med
                break
        if hit is None:
            return ToolResult(content="",
                              error=f"medication '{medication}' not found in EHR")
        save_record(record, ctx)
        return ToolResult(
            content=f"Recorded dose of {medication} at {_fmt_ts(ts)}.",
            data={"patient_id": patient_id, "medication": medication,
                  "last_dose_at": ts},
        )


@tool
class SendDoctorAlertTool(Tool):
    name = "send_doctor_alert"
    description = (
        "Send a medication / patient-state alert to the responsible doctor by "
        "email or SMS. Computes due medications from the EHR, drafts the "
        "message, and delivers it (dry-run if no SMTP/Twilio credentials are "
        "configured)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "patient_id": {"type": "string"},
            "channel": {"type": "string", "enum": ["email", "sms"]},
            "to": {"type": "string",
                   "description": "Doctor's email address or phone number."},
            "language": {"type": "string", "enum": ["en", "fa"]},
            "only_if_due": {"type": "boolean",
                            "description": "Send only when at least one dose is due (default true)."},
        },
        "required": ["patient_id", "channel", "to"],
    }

    async def run(self, ctx: ToolContext, patient_id: str, channel: str,
                  to: str, language: str = "en",
                  only_if_due: bool = True) -> ToolResult:
        record = load_record(patient_id, ctx)
        if record is None:
            return ToolResult(content="", error=f"no EHR for id {patient_id}")
        due = compute_due_medications(record)
        due_count = sum(1 for d in due if d.get("due"))
        if only_if_due and due_count == 0:
            return ToolResult(
                content="No doses due — alert not sent.",
                data={"patient_id": patient_id, "sent": False, "due_count": 0},
            )
        body = build_alert_text(record, due, language=language)
        name = (record.get("patient") or {}).get("name") or patient_id
        subject = f"[Patient Alert] {name} — {due_count} dose(s) due"

        if channel == "email":
            delivery = send_email(to=to, subject=subject, body=body)
        else:
            delivery = send_sms(to=to, body=f"{subject}\n\n{body}")

        return ToolResult(
            content=("Alert " + ("queued (dry-run)" if delivery.get("dry_run")
                                 else "sent") + f" via {channel} to {to}."),
            data={"patient_id": patient_id, "sent": True,
                  "due_count": due_count, "delivery": delivery, "body": body},
        )
