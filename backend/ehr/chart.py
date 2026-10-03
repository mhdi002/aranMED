"""Assemble a patient's unified chart: local EHR + PACS + other hospitals.

``build`` returns one structure the UI, the agent and the transfer package
all use. Every item is tagged with ``source`` — the facility that authored
it and whether it is held locally or was fetched live from a peer — so a
clinician can always tell what came from where.

Remote data comes from registered *remote sources* (the interop federation
registers one that queries peer hospitals' FHIR servers and DICOMweb).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from clinicaldb import facilities, mpi, settings
from ehr import store

log = logging.getLogger("ehr.chart")

SECTIONS = ("encounter", "condition", "allergy", "medication", "observation", "procedure",
            "immunization", "document", "service_request", "diagnostic_report", "consent")

_remote_sources: list[Callable[[dict, dict], dict]] = []


def register_remote_source(fn: Callable[[dict, dict], dict]) -> None:
    """``fn(person, principal) -> {"items": {section: [...]}, "imaging": [...],
    "errors": [...], "facilities": [...]}``"""
    if fn not in _remote_sources:
        _remote_sources.append(fn)


def _fac_name(oid: Optional[str], cache: dict) -> str:
    if not oid:
        return ""
    if oid not in cache:
        f = facilities.get_by_oid(oid)
        cache[oid] = f["name"] if f else oid
    return cache[oid]


def _strip(item: dict) -> dict:
    out = {k: v for k, v in item.items() if k not in ("deleted",)}
    if out.get("resource_type") == "document":
        out.pop("content", None)  # metadata only in the chart; content on demand
    return out


def build(person_id: str, *, principal: Optional[dict] = None, include_remote: bool = False,
          observation_limit: int = 500) -> Optional[dict[str, Any]]:
    person = mpi.get(person_id)
    if not person:
        return None
    pid = person["id"]
    names: dict[str, str] = {}
    local_oid = settings.facility_oid()
    chart: dict[str, Any] = {"person": person, "sections": {}, "imaging": [], "errors": [],
                             "facilities": [local_oid]}
    for sec in SECTIONS:
        items = store.list_for(sec, pid, limit=observation_limit if sec == "observation" else 500)
        for it in items:
            src = it.get("source_facility") or local_oid
            it["source"] = {"facility_oid": src, "facility": _fac_name(src, names),
                            "held": "local", "remote": src != local_oid}
        chart["sections"][sec] = [_strip(i) for i in items]

    try:
        from pacs import index as pacs_index
        for s in pacs_index.query_studies({"x-person-id": pid}, limit=200):
            src = s["_ext"].get("origin_facility") or local_oid
            s["source"] = {"facility_oid": src, "facility": _fac_name(src, names),
                           "held": "local", "remote": src != local_oid}
            chart["imaging"].append(s)
    except Exception:  # noqa: BLE001
        log.exception("imaging section failed")

    if include_remote and principal is not None:
        for fn in _remote_sources:
            try:
                res = fn(person, principal) or {}
            except Exception as e:  # noqa: BLE001
                chart["errors"].append({"source": getattr(fn, "__name__", "remote"), "error": str(e)})
                continue
            known = {(i.get("source_facility"), i.get("source_id"))
                     for sec in chart["sections"].values() for i in sec}
            for sec, items in (res.get("items") or {}).items():
                for it in items:
                    key = (it.get("source_facility"), it.get("source_id"))
                    if key in known and key != (None, None):
                        continue  # already imported locally (e.g. after a transfer)
                    chart["sections"].setdefault(sec, []).append(it)
            local_uids = {s["StudyInstanceUID"] for s in chart["imaging"]}
            chart["imaging"] += [s for s in res.get("imaging") or []
                                 if s.get("StudyInstanceUID") not in local_uids]
            chart["errors"] += res.get("errors") or []
            chart["facilities"] += [f for f in res.get("facilities") or []
                                    if f not in chart["facilities"]]
    chart["summary"] = summarize(chart)
    chart["facility_names"] = {oid: _fac_name(oid, names) for oid in chart["facilities"]}
    return chart


def _latest(items: list[dict], key: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for it in items:
        k = it.get("code") or it.get("display") or it.get("text")
        if not k:
            continue
        if k not in out or (it.get(key) or "") > (out[k].get(key) or ""):
            out[k] = it
    return out


def summarize(chart: dict) -> dict[str, Any]:
    s = chart["sections"]
    obs = s.get("observation", [])
    vitals = _latest([o for o in obs if o.get("category") == "vital-signs"], "effective")
    # Latest result per test; a test is abnormal if its latest value is.
    labs = list(_latest([o for o in obs if o.get("category") == "laboratory"], "effective").values())
    abnormal = [o for o in labs if (o.get("interpretation") or "N") not in ("N", "", None)]
    return {
        "active_problems": [c for c in s.get("condition", [])
                            if (c.get("clinical_status") or "active") == "active"],
        "allergies": s.get("allergy", []),
        "active_medications": [m for m in s.get("medication", [])
                               if (m.get("status") or "active") in ("active", "on-hold")],
        "latest_vitals": list(vitals.values()),
        "abnormal_labs": abnormal[:20],
        "last_encounter": (s.get("encounter") or [None])[0],
        "imaging_count": len(chart.get("imaging") or []),
        "remote_items": sum(1 for sec in s.values() for i in sec
                            if (i.get("source") or {}).get("held") == "remote"),
    }


def timeline(chart: dict) -> list[dict]:
    events = []
    spec_dates = {k: v.get("date") for k, v in store.RESOURCES.items()}
    for sec, items in chart["sections"].items():
        if sec in ("consent",):
            continue
        for it in items:
            when = it.get(spec_dates.get(sec) or "") or it.get("created_at")
            if isinstance(when, (int, float)):
                import time as _t
                when = _t.strftime("%Y-%m-%d", _t.gmtime(when))
            label = it.get("display") or it.get("text") or it.get("title") or it.get("type_text") or sec
            if sec == "observation" and (it.get("value_num") is not None or it.get("value_text")):
                label = f"{label}: {it.get('value_num') if it.get('value_num') is not None else it.get('value_text')} {it.get('unit') or ''}".strip()
            events.append({"date": when or "", "kind": sec, "label": label, "id": it.get("id"),
                           "source": it.get("source")})
    for s in chart.get("imaging") or []:
        d = s.get("StudyDate") or ""
        events.append({"date": f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d,
                       "kind": "imaging",
                       "label": s.get("StudyDescription") or ",".join(s.get("ModalitiesInStudy") or []),
                       "id": s.get("StudyInstanceUID"), "source": s.get("source")})
    events.sort(key=lambda e: e["date"] or "", reverse=True)
    return events


def as_text(chart: dict, *, max_items: int = 15) -> str:
    """Compact plain-text rendering for the LLM / transfer summaries."""
    p = chart["person"]
    d = p["demographics"]
    lines = [f"Patient: {p['name']} sex={d.get('sex') or '?'} DOB={d.get('birth_date') or '?'}",
             "Identifiers: " + ", ".join(f"{i['value']} ({i['system']})" for i in p["identifiers"][:6])]
    sm = chart["summary"]

    def fmt(items, f):
        return [f"  - {f(i)}" + (f" [{i['source']['facility']}]" if i.get("source") else "")
                for i in items[:max_items]]
    lines.append("Active problems:")
    lines += fmt(sm["active_problems"], lambda c: c.get("display") or c.get("text") or "?")
    lines.append("Allergies:")
    lines += fmt(sm["allergies"], lambda a: f"{a.get('display') or a.get('text')} ({a.get('reaction') or 'reaction n/a'})")
    lines.append("Medications:")
    lines += fmt(sm["active_medications"], lambda m: " ".join(x for x in (m.get("display") or m.get("text"), m.get("dose"), m.get("route"), m.get("frequency")) if x))
    lines.append("Latest vitals:")
    lines += fmt(sm["latest_vitals"], lambda o: f"{o.get('display')}: {o.get('value_num') if o.get('value_num') is not None else o.get('value_text')} {o.get('unit') or ''}")
    lines.append("Abnormal labs:")
    lines += fmt(sm["abnormal_labs"], lambda o: f"{o.get('display')}: {o.get('value_num')} {o.get('unit') or ''} ({o.get('interpretation')})")
    lines.append("Encounters:")
    lines += fmt(chart["sections"].get("encounter", []), lambda e: f"{e.get('start_at') or '?'} {e.get('class') or ''} {e.get('reason') or e.get('type_text') or ''}")
    lines.append("Imaging:")
    lines += fmt(chart.get("imaging") or [], lambda s: f"{s.get('StudyDate')} {','.join(s.get('ModalitiesInStudy') or [])} {s.get('StudyDescription') or ''}")
    reports = chart["sections"].get("diagnostic_report", [])
    if reports:
        lines.append("Reports:")
        lines += fmt(reports, lambda r: f"{r.get('effective') or ''} {r.get('display') or ''}: {(r.get('conclusion') or r.get('text') or '')[:300]}")
    return "\n".join(lines)
