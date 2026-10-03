"""PACS tools for the agent: find imaging, read it, draft reports from it.

These give the core LLM the same imaging access a clinician has in the
viewer, under the same rules:

* **Authenticated only.** Every tool requires ``ctx.owner_user_id`` and the
  caller's role must hold the matching permission (``pacs.read`` /
  ``pacs.write``) — chat used anonymously cannot reach patient imaging.
* **Audited.** Each call writes an audit row naming the user and study.
* **Drafts, not verdicts.** ``pacs_analyze_study`` renders key images with
  proper windowing, has the vision model describe them, structures that
  into the matching report template, and saves it as a *draft* linked to
  the study. Finalising stays a human action.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import audit
import auth
import rbac

from .base import Tool, ToolContext, ToolResult, registry as tool_registry, tool

log = logging.getLogger("tools.pacs")


def _user(ctx: ToolContext, permission: str) -> tuple[Optional[dict], Optional[str]]:
    if not ctx.owner_user_id:
        return None, "imaging access requires a signed-in user"
    user = auth.get_user(int(ctx.owner_user_id))
    if not user:
        return None, "unknown user"
    if not rbac.allows((user.get("role") or "").lower(), permission):
        audit.record("rbac.deny", actor=user, resource=permission, outcome="deny",
                     detail={"via": "agent"})
        return None, f"your role is not permitted to {permission}"
    return user, None


def _fmt_study(s: dict) -> str:
    mods = ",".join(s.get("ModalitiesInStudy") or []) or "?"
    date = s.get("StudyDate") or "?"
    if len(date) == 8:
        date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    where = ""
    if s.get("locations"):
        where = " @ " + "/".join(sorted({l["name"] for l in s["locations"]}))
    return (f"- {date} {mods} \"{s.get('StudyDescription') or ''}\" "
            f"patient={s.get('PatientName') or '?'} id={s.get('PatientID') or '?'} "
            f"acc={s.get('AccessionNumber') or '-'} "
            f"images={s.get('NumberOfStudyRelatedInstances') or '?'} "
            f"status={(s.get('_ext') or {}).get('status') or '-'}{where} "
            f"uid={s['StudyInstanceUID']}")


@tool
class PacsSearchStudies(Tool):
    name = "pacs_search_studies"
    description = ("Search imaging studies in the PACS (and, with scope='all', in peer "
                   "hospitals and connected archives). Filter by patient name, patient ID / "
                   "MRN, accession number, modality (CT, MR, US, CR, DX, MG...) and date range "
                   "(YYYY-MM-DD). Returns study UIDs to use with the other pacs_* tools.")
    parameters = {
        "type": "object",
        "properties": {
            "patient_name": {"type": "string"},
            "patient_id": {"type": "string"},
            "accession": {"type": "string"},
            "modality": {"type": "string"},
            "date_from": {"type": "string"},
            "date_to": {"type": "string"},
            "person_id": {"type": "string", "description": "MPI enterprise person id"},
            "scope": {"type": "string", "enum": ["local", "all"]},
            "limit": {"type": "integer"},
        },
        "required": [],
    }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        user, err = _user(ctx, "pacs.read")
        if err:
            return ToolResult(content="", error=err)
        from starlette.concurrency import run_in_threadpool
        from pacs import federation, index
        f: dict[str, str] = {}
        if kw.get("patient_name"):
            n = kw["patient_name"]
            f["PatientName"] = n if "*" in n else f"*{n}*"
        if kw.get("patient_id"):
            f["PatientID"] = kw["patient_id"]
        if kw.get("accession"):
            f["AccessionNumber"] = kw["accession"]
        if kw.get("modality"):
            f["ModalitiesInStudy"] = kw["modality"].upper()
        if kw.get("date_from") or kw.get("date_to"):
            f["StudyDate"] = (f"{(kw.get('date_from') or '').replace('-', '')}-"
                              f"{(kw.get('date_to') or '').replace('-', '')}")
        if kw.get("person_id"):
            f["x-person-id"] = kw["person_id"]
        limit = max(1, min(int(kw.get("limit") or 10), 50))
        if (kw.get("scope") or "local") == "all":
            res = await run_in_threadpool(federation.search, f, scope="all", limit=limit,
                                          practitioner=user["username"])
            studies, errors = res["studies"], res["errors"]
        else:
            studies = await run_in_threadpool(index.query_studies, f, limit=limit)
            errors = []
        audit.record("pacs.search", actor=user, resource="studies",
                     detail={"via": "agent", "results": len(studies)})
        if not studies:
            text = "No matching imaging studies."
        else:
            text = f"{len(studies)} study(ies):\n" + "\n".join(_fmt_study(s) for s in studies)
        if errors:
            text += "\n(unreachable: " + ", ".join(e["node"] for e in errors) + ")"
        return ToolResult(content=text, data={"studies": studies, "errors": errors})


@tool
class PacsGetStudy(Tool):
    name = "pacs_get_study"
    description = ("Details of one imaging study: series (modality, description, image "
                   "count), linked reports, worklist/order info, and prior studies of the "
                   "same patient.")
    parameters = {"type": "object",
                  "properties": {"study_uid": {"type": "string"}},
                  "required": ["study_uid"]}

    async def run(self, ctx: ToolContext, study_uid: str) -> ToolResult:
        user, err = _user(ctx, "pacs.read")
        if err:
            return ToolResult(content="", error=err)
        from pacs import index, reports, worklist
        s = index.get_study(study_uid)
        if not s:
            return ToolResult(content="", error=f"study {study_uid} not found")
        series = index.query_series({"StudyInstanceUID": study_uid})
        reps = reports.list_for_study(study_uid)
        priors = []
        pid = (s.get("_ext") or {}).get("person_id")
        if pid:
            priors = [p for p in index.query_studies({"x-person-id": pid}, limit=20)
                      if p["StudyInstanceUID"] != study_uid]
        wl = worklist.get(s["AccessionNumber"]) if s.get("AccessionNumber") else None
        audit.record("pacs.study.read", actor=user, resource=f"study:{study_uid}",
                     detail={"via": "agent"})
        lines = [_fmt_study(s), "Series:"]
        lines += [f"  #{x.get('SeriesNumber')}: {x.get('Modality')} "
                  f"\"{x.get('SeriesDescription') or ''}\" "
                  f"{x.get('NumberOfSeriesRelatedInstances')} images "
                  f"(body part {x.get('BodyPartExamined') or '?'}) uid={x['SeriesInstanceUID']}"
                  for x in series]
        if wl:
            lines.append(f"Order: {wl.get('procedure_description') or ''} "
                         f"priority={wl.get('priority') or '-'} reason={wl.get('reason') or '-'}")
        if reps:
            lines.append(f"Latest report ({reps[0]['status']}):\n{reps[0]['text']}")
        else:
            lines.append("No report yet.")
        if priors:
            lines.append("Priors:\n" + "\n".join(_fmt_study(p) for p in priors[:5]))
        ctx.state["study_uid"] = study_uid
        return ToolResult(content="\n".join(lines),
                          data={"study": s, "series": series, "reports": reps,
                                "priors": priors, "worklist": wl})


def _key_images(study_uid: str, series_uid: Optional[str], max_images: int) -> list[dict]:
    from pacs import index
    insts = [i for i in index.query_instances(
        {"StudyInstanceUID": study_uid, **({"SeriesInstanceUID": series_uid} if series_uid else {})},
        limit=10000) if i.get("Rows")]
    if not insts:
        return []
    by_series: dict[str, list[dict]] = {}
    for i in insts:
        by_series.setdefault(i["SeriesInstanceUID"], []).append(i)
    # Middle slice of each series, largest series first.
    picks = []
    for rows in sorted(by_series.values(), key=len, reverse=True):
        picks.append(rows[len(rows) // 2])
        if len(picks) >= max_images:
            break
    return picks


@tool
class PacsAnalyzeStudy(Tool):
    name = "pacs_analyze_study"
    description = ("AI-assisted read of an imaging study: renders key images (correct "
                   "windowing), asks the vision model for findings, structures them into the "
                   "matching report template and saves a DRAFT report on the study for a "
                   "radiologist to review. Use when asked to analyse/read/report a study.")
    parameters = {
        "type": "object",
        "properties": {
            "study_uid": {"type": "string"},
            "series_uid": {"type": "string"},
            "question": {"type": "string",
                         "description": "Clinical question / focus (e.g. 'rule out PE')"},
            "max_images": {"type": "integer"},
            "template_id": {"type": "string"},
            "save_draft": {"type": "boolean"},
        },
        "required": ["study_uid"],
    }

    async def run(self, ctx: ToolContext, study_uid: str, series_uid: Optional[str] = None,
                  question: Optional[str] = None, max_images: int = 3,
                  template_id: Optional[str] = None, save_draft: bool = True) -> ToolResult:
        user, err = _user(ctx, "pacs.read")
        if err:
            return ToolResult(content="", error=err)
        from starlette.concurrency import run_in_threadpool
        from pacs import index, render, reports, worklist
        from providers.base import ChatMessage

        s = index.get_study(study_uid)
        if not s:
            return ToolResult(content="", error=f"study {study_uid} not found")
        picks = await run_in_threadpool(_key_images, study_uid, series_uid,
                                        max(1, min(int(max_images or 3), 6)))
        if not picks:
            return ToolResult(content="", error="study has no renderable images")
        images = []
        for i, inst in enumerate(picks):
            data = index.read_instance(inst["SOPInstanceUID"])
            frame = max(1, (inst.get("NumberOfFrames") or 1) // 2 or 1)
            png = await run_in_threadpool(render.render, data, frame=frame, fmt="png",
                                          max_size=1024)
            key = f"image:pacs:{i}"
            ctx.attachments[key] = png
            images.append(png)

        wl = worklist.get(s["AccessionNumber"]) if s.get("AccessionNumber") else None
        mods = ",".join(s.get("ModalitiesInStudy") or [])
        parts = (s.get("_ext") or {}).get("body_parts") or []
        context = (f"Study: {mods} {s.get('StudyDescription') or ''} "
                   f"(body part: {', '.join(parts) or 'unspecified'}). "
                   f"Patient sex {s.get('PatientSex') or '?'}, born {s.get('PatientBirthDate') or '?'}. ")
        if wl and (wl.get("reason") or wl.get("procedure_description")):
            context += f"Order: {wl.get('procedure_description') or ''}; reason: {wl.get('reason') or ''}. "
        if question:
            context += f"Clinical question: {question}. "
        prompt = (context + f"{len(images)} representative image(s) follow (one per series, "
                  "middle slice). Describe the findings systematically, then give an impression. "
                  "Do not invent findings; state limitations of single-slice review.")
        vision = await ctx.registry.get_vision()
        sys = ChatMessage(role="system", content=(
            "You are a board-certified radiologist reviewing key images from a PACS study. "
            "Be precise, hedge appropriately, never invent findings, and recommend "
            "correlation with the full study."))
        out = await vision.chat([sys, ChatMessage(role="user", content=prompt, images=images)],
                                temperature=0.2, max_tokens=800)
        findings = out.content or ""
        ctx.state["vision_model"] = vision.name
        ctx.state["transcript"] = findings

        if not template_id:
            try:
                from template_selection import auto_select_template
                sug = auto_select_template(f"{mods} {s.get('StudyDescription') or ''} "
                                           f"{' '.join(parts)} {findings}")
                template_id = sug.template_id if sug else None
            except Exception:  # noqa: BLE001
                template_id = None
        report_text = findings
        report_data: dict[str, Any] = {}
        try:
            structured = await tool_registry.get("structure_report").run(
                ctx, transcript=findings, template_id=template_id,
                extra_context=context)
            if not structured.error and structured.data:
                report_text = structured.data.get("report") or findings
                report_data = structured.data
        except Exception as e:  # noqa: BLE001
            log.warning("structure_report failed during study analysis: %s", e)

        saved = None
        if save_draft:
            saved = reports.save(study_uid, text=report_text, status="draft", source="agent",
                                 author=f"agent:{user['username']}",
                                 data={"findings": findings, "template_id": template_id,
                                       "vision_model": vision.name,
                                       "images": [p["SOPInstanceUID"] for p in picks],
                                       "critical_alerts": report_data.get("critical_alerts")})
        audit.record("pacs.agent.analyze", actor=user, resource=f"study:{study_uid}",
                     detail={"images": len(images), "template": template_id,
                             "saved": bool(saved)})
        return ToolResult(
            content=(f"Draft report for {mods} study {study_uid}"
                     f"{' (template ' + template_id + ')' if template_id else ''}:\n{report_text}"
                     + ("\n[saved as DRAFT — requires radiologist review]" if saved else "")),
            data={"study_uid": study_uid, "findings": findings, "report": report_text,
                  "template_id": template_id, "report_id": saved["id"] if saved else None,
                  "images": [p["SOPInstanceUID"] for p in picks],
                  "critical_alerts": report_data.get("critical_alerts") or []})


@tool
class PacsComparePriors(Tool):
    name = "pacs_compare_priors"
    description = ("List the patient's prior imaging (this archive and, with "
                   "include_remote, peer hospitals) relevant to a study, with prior report "
                   "impressions, to support comparison.")
    parameters = {"type": "object",
                  "properties": {"study_uid": {"type": "string"},
                                 "include_remote": {"type": "boolean"}},
                  "required": ["study_uid"]}

    async def run(self, ctx: ToolContext, study_uid: str,
                  include_remote: bool = False) -> ToolResult:
        user, err = _user(ctx, "pacs.read")
        if err:
            return ToolResult(content="", error=err)
        from starlette.concurrency import run_in_threadpool
        from pacs import federation, index, reports
        s = index.get_study(study_uid)
        if not s:
            return ToolResult(content="", error=f"study {study_uid} not found")
        pid = (s.get("_ext") or {}).get("person_id")
        priors = [p for p in index.query_studies({"x-person-id": pid}, limit=50)
                  if p["StudyInstanceUID"] != study_uid] if pid else []
        errors = []
        if include_remote and s.get("PatientID"):
            res = await run_in_threadpool(federation.search, {"PatientID": s["PatientID"]},
                                          scope="remote", limit=50,
                                          practitioner=user["username"])
            known = {p["StudyInstanceUID"] for p in priors} | {study_uid}
            priors += [r for r in res["studies"] if r["StudyInstanceUID"] not in known]
            errors = res["errors"]
        lines = []
        for p in priors:
            line = _fmt_study(p)
            reps = reports.list_for_study(p["StudyInstanceUID"])
            if reps:
                line += f"\n    report ({reps[0]['status']}): {reps[0]['text'][:400]}"
            lines.append(line)
        audit.record("pacs.priors", actor=user, resource=f"study:{study_uid}",
                     detail={"via": "agent", "priors": len(priors)})
        text = ("Prior studies:\n" + "\n".join(lines)) if lines else "No prior studies found."
        return ToolResult(content=text, data={"priors": priors, "errors": errors})


@tool
class PacsLinkReport(Tool):
    name = "pacs_link_report"
    description = ("Attach report text to a study as a draft or preliminary report "
                   "(final sign-off is done by a radiologist in the UI).")
    parameters = {"type": "object",
                  "properties": {"study_uid": {"type": "string"},
                                 "text": {"type": "string"},
                                 "status": {"type": "string",
                                            "enum": ["draft", "preliminary"]}},
                  "required": ["study_uid"]}

    async def run(self, ctx: ToolContext, study_uid: str, text: Optional[str] = None,
                  status: str = "draft") -> ToolResult:
        user, err = _user(ctx, "pacs.write")
        if err:
            return ToolResult(content="", error=err)
        from pacs import reports
        text = text or ctx.state.get("report")
        if not text:
            return ToolResult(content="", error="no report text given or drafted in this turn")
        if status not in ("draft", "preliminary"):
            status = "draft"
        try:
            rep = reports.save(study_uid, text=text, status=status, source="agent",
                               author=f"agent:{user['username']}")
        except ValueError as e:
            return ToolResult(content="", error=str(e))
        audit.record("pacs.report.save", actor=user, resource=f"study:{study_uid}",
                     detail={"via": "agent", "status": status})
        return ToolResult(content=f"Report saved on study {study_uid} as {status}.",
                          data={"report": rep})


@tool
class PacsWorklist(Tool):
    name = "pacs_worklist"
    description = ("Show the imaging worklist (scheduled / in-progress procedures) "
                   "optionally filtered by status, modality and date (YYYY-MM-DD).")
    parameters = {"type": "object",
                  "properties": {"status": {"type": "string"}, "modality": {"type": "string"},
                                 "date": {"type": "string"}},
                  "required": []}

    async def run(self, ctx: ToolContext, status: Optional[str] = None,
                  modality: Optional[str] = None, date: Optional[str] = None) -> ToolResult:
        user, err = _user(ctx, "pacs.read")
        if err:
            return ToolResult(content="", error=err)
        from pacs import worklist
        items = worklist.list_entries(status=status, modality=modality, date=date)
        lines = [f"- {i.get('scheduled_start') or '?'} {i.get('modality')} "
                 f"{i.get('procedure_description') or ''} patient={i.get('patient_name') or '?'} "
                 f"acc={i['accession']} status={i['status']} priority={i.get('priority') or '-'}"
                 for i in items]
        return ToolResult(content=("Worklist:\n" + "\n".join(lines)) if lines
                          else "Worklist is empty for that filter.", data={"items": items})
