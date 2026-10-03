"""HL7 v2 inbound processing: messages in, MPI/EHR/PACS updates out, ACK back.

Handled message types (anything else is acknowledged ``AR``):

* ``ADT^A01/A04/A05/A08/A28/A31``  register/update the person (MPI) and the
  visit (Encounter); ``A02`` transfer (location), ``A03`` discharge,
  ``A11/A13`` cancel admit/discharge, ``A40`` merge (MRG = prior identifier).
* ``ORM^O01`` / ``OMI^O23`` / ``OMG^O19``  orders → ServiceRequest; imaging
  orders reach the modality worklist (via ehr.links); ``ORC-1 CA`` cancels.
* ``ORU^R01``  results → Observations (+ DiagnosticReport per OBR).
* ``MDM^T02/T04``  documents (TXA + OBX text) → DocumentReference.
* ``VXU^V04``  immunizations (RXA).
* ``QBP^Q22``  IHE PDQ query → ``RSP^K22``;  ``QBP^Q23``  IHE PIX → ``RSP^K23``.

Identifiers: PID-3 repetitions are CX values; the assigning authority's
universal id (an OID) becomes ``urn:oid:<oid>``, a bare namespace becomes
``urn:aranmed:aa:<namespace>``, and an identifier with none is attributed
to the sending facility (MSH-4) or ``HL7_DEFAULT_ASSIGNING_AUTHORITY``.
Type ``NI`` maps to the national-id system.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

import hl7v2
from clinicaldb import messages, mpi, settings
from ehr import store

log = logging.getLogger("interop.hl7")


class Hl7Reject(Exception):
    """Message is structurally unacceptable (ACK AR)."""


class Hl7Error(Exception):
    """Message understood but could not be applied (ACK AE)."""


_ESC = {"F": "|", "S": "^", "T": "&", "R": "~", "E": "\\", ".br": "\n"}


def unescape(value: str) -> str:
    return re.sub(r"\\(\.br|[FSTRE])\\", lambda m: _ESC.get(m.group(1), ""), value or "")


def _reps(msg: hl7v2.Hl7Message, raw: str) -> list[str]:
    return [r for r in (raw or "").split(msg.encoding["repeat"]) if r]


def _comps(msg: hl7v2.Hl7Message, raw: str) -> list[str]:
    return (raw or "").split(msg.encoding["component"])


def _sub(msg: hl7v2.Hl7Message, raw: str) -> list[str]:
    return (raw or "").split(msg.encoding["subcomponent"])


def _seg_field(msg, seg: list[str], idx: int) -> str:
    return seg[idx] if idx < len(seg) else ""


def _hl7_dt(v: str) -> Optional[str]:
    v = (v or "").strip()
    if len(v) >= 12:
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}T{v[8:10]}:{v[10:12]}:{v[12:14] or '00'}"
    if len(v) >= 8:
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}"
    return v or None


def _sending_authority(msg: hl7v2.Hl7Message) -> str:
    uid = msg.component("MSH", 4, 2)
    if uid and re.fullmatch(r"[0-9.]+", uid):
        return uid
    return settings.env("HL7_DEFAULT_ASSIGNING_AUTHORITY", "") or settings.facility_oid()


def cx_identifiers(msg: hl7v2.Hl7Message, raw: str) -> list[dict]:
    out = []
    for rep in _reps(msg, raw):
        c = _comps(msg, rep)
        value = c[0].strip() if c else ""
        if not value:
            continue
        aa = c[3] if len(c) > 3 else ""
        typ = (c[4] if len(c) > 4 else "") or "MR"
        sub = _sub(msg, aa)
        ns, uid = (sub[0] if sub else ""), (sub[1] if len(sub) > 1 else "")
        if typ == "NI":
            system, fac = settings.national_id_system(), None
        elif uid and re.fullmatch(r"[0-9.]+", uid):
            system, fac = f"urn:oid:{uid}", uid
        elif ns:
            system, fac = f"urn:aranmed:aa:{ns}", None
        else:
            fac = _sending_authority(msg)
            system = f"urn:oid:{fac}"
        out.append({"system": system, "value": value, "type": typ, "facility_oid": fac})
    return out


def pid_demographics(msg: hl7v2.Hl7Message) -> dict:
    name = _comps(msg, msg.field("PID", 5).split(msg.encoding["repeat"])[0])
    addr = _comps(msg, msg.field("PID", 11).split(msg.encoding["repeat"])[0])
    phone = _comps(msg, msg.field("PID", 13).split(msg.encoding["repeat"])[0])
    return {"family": unescape(name[0]) if name and name[0] else None,
            "given": " ".join(unescape(x) for x in name[1:3] if x) or None,
            "birth_date": msg.field("PID", 7)[:8] or None,
            "sex": {"M": "male", "F": "female", "O": "other", "U": "unknown"}.get(
                msg.field("PID", 8).upper()),
            "address": ", ".join(x for x in addr[:5] if x) or None,
            "phone": phone[0] if phone and phone[0] else None,
            "deceased": True if msg.field("PID", 30) == "Y" else None}


def _person(msg: hl7v2.Hl7Message, *, register: bool = True) -> str:
    idents = cx_identifiers(msg, msg.field("PID", 3))
    if not idents:
        raise Hl7Reject("PID-3 patient identifier list is empty")
    demo = pid_demographics(msg)
    if not register:
        # Documents must attach to a known patient, never create one.
        pid = mpi.find_by_any_identifier(idents)
        if not pid:
            raise Hl7Error("patient is not known (send ADT first)")
        return pid
    reg = mpi.register_person(demo, idents, source_facility=_sending_authority(msg))
    if msg.message_type.split("^")[1:2] in (["A08"], ["A31"]):
        mpi.update_demographics(reg["person_id"], demo)
    return reg["person_id"]


# ---------------------------------------------------------------- ADT
_PV1_CLASS = {"E": "EMER", "I": "IMP", "O": "AMB", "P": "PRENC", "R": "AMB", "B": "AMB"}


def _encounter_source(msg: hl7v2.Hl7Message) -> tuple[str, str]:
    visit = msg.component("PV1", 19, 1) or msg.control_id
    return _sending_authority(msg), f"hl7-visit:{visit}"


def handle_adt(msg: hl7v2.Hl7Message) -> dict:
    event = (msg.message_type.split(msg.encoding["component"]) + ["", ""])[1]
    if event == "A40":
        return _merge(msg)
    person = _person(msg)
    out: dict[str, Any] = {"person_id": person, "event": event}
    if msg.segment("PV1") and event not in ("A28", "A31"):
        fac, src = _encounter_source(msg)
        loc = _comps(msg, msg.field("PV1", 3))
        status = {"A01": "in-progress", "A04": "arrived", "A05": "planned", "A08": None,
                  "A02": "in-progress", "A03": "finished", "A11": "cancelled",
                  "A13": "in-progress"}.get(event)
        vals: dict[str, Any] = {
            "person_id": person, "source_facility": fac, "source_id": src,
            "class": _PV1_CLASS.get(msg.field("PV1", 2).upper(), "AMB"),
            "location": " ".join(x for x in loc[:3] if x) or None,
            "department": loc[3] if len(loc) > 3 and loc[3] else None,
            "attending": " ".join(x for x in _comps(msg, msg.field("PV1", 7))[1:3] if x) or None,
            "admit_source": msg.field("PV1", 14) or None,
            "start_at": _hl7_dt(msg.field("PV1", 44)) or _hl7_dt(msg.field("EVN", 2)),
            "reason": unescape(msg.component("PV2", 3, 2) or msg.component("PV2", 3, 1)) or None,
        }
        if event == "A03":
            vals["end_at"] = _hl7_dt(msg.field("PV1", 45)) or _hl7_dt(msg.field("EVN", 2))
            vals["disposition"] = msg.field("PV1", 36) or None
        if status:
            vals["status"] = status
        enc = store.create("encounter", {k: v for k, v in vals.items() if v is not None},
                           actor="hl7")
        out["encounter_id"] = enc["id"]
    for i, al in enumerate(msg.all_segments("AL1")):
        allergen = _comps(msg, _seg_field(msg, al, 3))
        store.create("allergy", {"person_id": person, "source_facility": _sending_authority(msg),
                                 "source_id": f"hl7-al1:{person}:{allergen[0] if allergen else i}",
                                 "display": unescape(allergen[1] if len(allergen) > 1 else allergen[0]),
                                 "code": allergen[0] or None,
                                 "severity": {"SV": "severe", "MO": "moderate", "MI": "mild"}.get(_seg_field(msg, al, 4)),
                                 "reaction": unescape(_seg_field(msg, al, 5)) or None,
                                 "status": "active"}, actor="hl7")
    return out


def _merge(msg: hl7v2.Hl7Message) -> dict:
    survivor = _person(msg)
    prior = cx_identifiers(msg, msg.field("MRG", 1))
    if not prior:
        raise Hl7Reject("A40 requires MRG-1 prior patient identifier")
    old = mpi.find_by_any_identifier(prior)
    if not old:
        raise Hl7Error("MRG-1 prior identifier is not known")
    res = mpi.merge(survivor, old, reason=f"ADT^A40 {msg.control_id}", actor="hl7")
    return {"person_id": res["survivor"], "merged": res["merged"], "event": "A40"}


# ---------------------------------------------------------------- orders
def _tq_start(msg: hl7v2.Hl7Message, orc: list[str]) -> str:
    """ORC-7 quantity/timing, component 4 = start date/time."""
    tq = _comps(msg, _seg_field(msg, orc, 7))
    return tq[3] if len(tq) > 3 else ""


def _obr_order(msg: hl7v2.Hl7Message, orc: list[str], obr: list[str], person: str) -> dict:
    placer = _comps(msg, _seg_field(msg, obr, 2) or _seg_field(msg, orc, 2))[0]
    filler = _comps(msg, _seg_field(msg, obr, 3) or _seg_field(msg, orc, 3))[0]
    svc = _comps(msg, _seg_field(msg, obr, 4))
    diag_svc = _seg_field(msg, obr, 24).upper()
    modality = None
    ipc = msg.segment("IPC")
    if ipc:
        modality = _seg_field(msg, ipc, 5) or None
    if not modality and diag_svc in ("CT", "MR", "US", "CR", "DX", "NM", "MG", "XA", "RF", "PT"):
        modality = diag_svc
    category = "imaging" if (modality or diag_svc in ("RAD", "RX") or msg.message_type.startswith("OMI")) \
        else "laboratory" if diag_svc in ("LAB", "CH", "HM", "MB") else "procedure"
    prio = (_comps(msg, _seg_field(msg, orc, 7))[5:6] or [""])[0] or \
        (_comps(msg, _seg_field(msg, obr, 27))[5:6] or [""])[0]
    return {
        "person_id": person, "source_facility": _sending_authority(msg),
        "source_id": f"hl7-order:{placer or filler or msg.control_id}",
        "category": category, "intent": "order", "status": "active",
        "code": svc[0] or None, "display": unescape(svc[1] if len(svc) > 1 else svc[0]) or None,
        "code_system": "urn:aranmed:hl7:local" if svc and svc[0] else None,
        "priority": {"S": "stat", "A": "asap", "R": "routine", "U": "urgent"}.get(prio.upper(), None),
        "reason": unescape(_comps(msg, _seg_field(msg, obr, 31))[1] if len(_comps(msg, _seg_field(msg, obr, 31))) > 1
                           else _seg_field(msg, obr, 13)) or None,
        "requester": " ".join(x for x in _comps(msg, _seg_field(msg, obr, 16))[1:3] if x) or None,
        "occurrence": _hl7_dt(_seg_field(msg, obr, 7) or _tq_start(msg, orc)),
        "accession": filler or None,
        "data": {"modality": modality, "placer": placer, "filler": filler,
                 "station_ae": settings.env("HL7_DEFAULT_STATION_AE", "") or None},
    }


def handle_order(msg: hl7v2.Hl7Message) -> dict:
    person = _person(msg)
    out = {"person_id": person, "orders": []}
    orcs = msg.all_segments("ORC")
    obrs = msg.all_segments("OBR")
    for i, obr in enumerate(obrs):
        orc = orcs[i] if i < len(orcs) else (orcs[0] if orcs else ["ORC", "NW"])
        control = _seg_field(msg, orc, 1).upper() or "NW"
        vals = _obr_order(msg, orc, obr, person)
        if control in ("CA", "OC", "DC"):
            existing = store.search("service_request", {"source_facility": vals["source_facility"],
                                                         "source_id": vals["source_id"]})
            for sr in existing:
                store.update("service_request", sr["id"], {"status": "revoked"}, actor="hl7")
                if sr.get("accession"):
                    from pacs import worklist
                    wl = worklist.get(sr["accession"])
                    if wl and wl["status"] in ("scheduled", "in_progress"):
                        worklist.update(wl["id"], {"status": "cancelled"})
            out["orders"].append({"cancelled": vals["source_id"]})
            continue
        if vals.get("accession") is None:
            vals.pop("accession")
        sr = store.create("service_request", {k: v for k, v in vals.items() if v is not None},
                          actor="hl7")
        out["orders"].append({"id": sr["id"], "accession": store.get("service_request", sr["id"]).get("accession")})
    return out


# ---------------------------------------------------------------- results
def _ref_range(s: str) -> tuple[Optional[float], Optional[float]]:
    m = re.match(r"\s*([-\d.]+)?\s*-\s*([-\d.]+)?\s*$", s or "")
    if not m:
        return None, None
    lo = float(m.group(1)) if m.group(1) else None
    hi = float(m.group(2)) if m.group(2) else None
    return lo, hi


def handle_oru(msg: hl7v2.Hl7Message) -> dict:
    person = _person(msg)
    fac = _sending_authority(msg)
    out = {"person_id": person, "observations": 0, "reports": 0}
    current_obr: Optional[list[str]] = None
    groups: list[tuple[list[str], list[list[str]]]] = []
    for seg in msg.segments:
        if seg[0] == "OBR":
            current_obr = seg
            groups.append((seg, []))
        elif seg[0] == "OBX":
            if not groups:
                groups.append((["OBR"], []))
            groups[-1][1].append(seg)
    for obr, obxs in groups:
        svc = _comps(msg, _seg_field(msg, obr, 4))
        diag = _seg_field(msg, obr, 24).upper()
        filler = _comps(msg, _seg_field(msg, obr, 3))[0] or _comps(msg, _seg_field(msg, obr, 2))[0] or msg.control_id
        effective = _hl7_dt(_seg_field(msg, obr, 7)) or _hl7_dt(msg.field("MSH", 7))
        category = "RAD" if diag in ("RAD", "CT", "MR", "US", "CR") else "LAB"
        obs_ids, text_lines = [], []
        for obx in obxs:
            vtype = _seg_field(msg, obx, 2)
            code = _comps(msg, _seg_field(msg, obx, 3))
            value = _seg_field(msg, obx, 5)
            if vtype in ("TX", "FT", "ST") and (category == "RAD" or (code and "GDT" in code[0])):
                text_lines.append(unescape(value))
                continue
            lo, hi = _ref_range(_seg_field(msg, obx, 7))
            num = None
            if vtype in ("NM", "SN"):
                try:
                    num = float(value.split(msg.encoding["component"])[-1])
                except ValueError:
                    num = None
            flag = _seg_field(msg, obx, 8).upper() or None
            row = store.create("observation", {
                "person_id": person, "source_facility": fac,
                "source_id": f"hl7-obx:{filler}:{code[0] if code else ''}:{_seg_field(msg, obx, 4) or '1'}",
                "category": "laboratory" if category == "LAB" else "imaging",
                "code_system": "http://loinc.org" if len(code) > 2 and code[2] in ("LN", "LOINC") else
                ("urn:aranmed:hl7:local" if code and code[0] else None),
                "code": code[0] or None, "display": unescape(code[1] if len(code) > 1 else code[0]),
                "value_num": num, "value_text": None if num is not None else unescape(value),
                "unit": _comps(msg, _seg_field(msg, obx, 6))[0] or None,
                "ref_low": lo, "ref_high": hi, "interpretation": flag,
                "status": {"F": "final", "P": "preliminary", "C": "amended", "X": "cancelled"}.get(
                    _seg_field(msg, obx, 11).upper(), "final"),
                "effective": _hl7_dt(_seg_field(msg, obx, 14)) or effective}, actor="hl7")
            obs_ids.append(row["id"])
            out["observations"] += 1
        status = {"F": "final", "P": "preliminary", "C": "corrected", "X": "cancelled"}.get(
            _seg_field(msg, obr, 25).upper(), "final")
        store.create("diagnostic_report", {
            "person_id": person, "source_facility": fac, "source_id": f"hl7-obr:{filler}",
            "category": category, "status": status,
            "code": svc[0] or None, "display": unescape(svc[1] if len(svc) > 1 else svc[0]) or "Result",
            "effective": effective, "result_ids": obs_ids,
            "text": "\n".join(text_lines) or None,
            "conclusion": (("\n".join(text_lines)).split("IMPRESSION:", 1)[-1].strip()[:2000]
                           if text_lines else None)}, actor="hl7")
        out["reports"] += 1
    return out


def handle_mdm(msg: hl7v2.Hl7Message) -> dict:
    person = _person(msg, register=False)
    txa = msg.segment("TXA") or []
    doc_type = _seg_field(msg, txa, 2) or "progress-note"
    unique = _comps(msg, _seg_field(msg, txa, 12))[0] or msg.control_id
    text = "\n".join(unescape(_seg_field(msg, obx, 5)) for obx in msg.all_segments("OBX"))
    mapped = {"DS": "discharge-summary", "PN": "progress-note", "CN": "consult-note",
              "RN": "referral-note", "TS": "transfer-summary"}.get(doc_type.upper(), doc_type.lower())
    doc = store.create("document", {
        "person_id": person, "source_facility": _sending_authority(msg),
        "source_id": f"hl7-doc:{unique}", "doc_type": mapped,
        "title": unescape(_seg_field(msg, txa, 16)) or mapped.replace("-", " ").title(),
        "author": " ".join(x for x in _comps(msg, _seg_field(msg, txa, 9))[1:3] if x) or None,
        "content_type": "text/plain", "content": text,
        "effective": _hl7_dt(_seg_field(msg, txa, 4)) or _hl7_dt(msg.field("MSH", 7)),
        "status": "current"}, actor="hl7")
    return {"person_id": person, "document_id": doc["id"]}


def handle_vxu(msg: hl7v2.Hl7Message) -> dict:
    person = _person(msg)
    n = 0
    for rxa in msg.all_segments("RXA"):
        vac = _comps(msg, _seg_field(msg, rxa, 5))
        store.create("immunization", {
            "person_id": person, "source_facility": _sending_authority(msg),
            "source_id": f"hl7-rxa:{person}:{vac[0] if vac else ''}:{_seg_field(msg, rxa, 3)}",
            "code_system": "http://hl7.org/fhir/sid/cvx" if len(vac) > 2 and vac[2] == "CVX" else None,
            "code": vac[0] or None, "display": unescape(vac[1] if len(vac) > 1 else vac[0]),
            "occurrence": _hl7_dt(_seg_field(msg, rxa, 3)), "lot": _seg_field(msg, rxa, 15) or None,
            "status": "completed"}, actor="hl7")
        n += 1
    return {"person_id": person, "immunizations": n}


# ---------------------------------------------------------------- queries
def _qpd_params(msg: hl7v2.Hl7Message) -> dict[str, str]:
    out = {}
    for rep in _reps(msg, msg.field("QPD", 3)):
        c = _comps(msg, rep)
        if len(c) >= 2:
            out[c[0].lstrip("@")] = c[1]
    return out


def _pid_segment(person: dict, idx: int) -> str:
    d = person["demographics"]
    ids = "~".join(f"{i['value']}^^^&{i['system'][8:] if i['system'].startswith('urn:oid:') else ''}&ISO^{i.get('type') or 'MR'}"
                   for i in person["identifiers"])
    sex = {"male": "M", "female": "F", "other": "O"}.get(d.get("sex") or "", "U")
    return "|".join(["PID", str(idx), "", ids, "", f"{d.get('family') or ''}^{d.get('given') or ''}", "",
                     (d.get("birth_date") or "").replace("-", ""), sex])


def handle_qbp(msg: hl7v2.Hl7Message) -> str:
    event = (msg.message_type.split(msg.encoding["component"]) + ["", ""])[1]
    qtag = _comps(msg, msg.field("QPD", 2))[0]
    ts = time.strftime("%Y%m%d%H%M%S")
    header = "|".join(["MSH", "^~\\&", settings.env("HL7_SENDING_APP", "ARANMED"),
                       settings.facility_oid(), msg.field("MSH", 3), msg.field("MSH", 4), ts, "",
                       f"RSP^{'K22' if event == 'Q22' else 'K23'}^RSP_{'K21' if event == 'Q22' else 'K23'}",
                       f"RSP{ts}", "P", "2.5"])
    persons: list[dict] = []
    if event == "Q23":
        idents = cx_identifiers(msg, msg.field("QPD", 3))
        pid = mpi.find_by_any_identifier(idents)
        if pid:
            persons = [mpi.get(pid)]
    else:
        prm = _qpd_params(msg)
        hits = mpi.search(family=prm.get("PID.5.1"), given=prm.get("PID.5.2"),
                          birth_date=prm.get("PID.7"), identifier=prm.get("PID.3.1"))
        persons = [mpi.get(h["person_id"]) for h in hits]
    qak = "|".join(["QAK", qtag or "", "OK" if persons else "NF"])
    segs = [header, f"MSA|AA|{msg.control_id}", qak, "|".join(msg.segment("QPD") or ["QPD"])]
    segs += [_pid_segment(p, i + 1) for i, p in enumerate(persons)]
    return "\r".join(segs)


# ---------------------------------------------------------------- dispatcher
def process(raw: str, *, peer: Optional[str] = None) -> str:
    """Process one inbound message and return the ACK/response text."""
    try:
        msg = hl7v2.parse(raw)
    except ValueError as e:
        messages.log(direction="in", protocol="hl7v2", status="rejected", peer=peer,
                     payload=raw, error=str(e))
        fake = hl7v2.Hl7Message(segments=[["MSH", "^~\\&", "", "", "", "", "", "", "", ""]])
        return hl7v2.build_ack(fake, code="AR", text=str(e)[:80])
    mtype = msg.message_type.split(msg.encoding["component"])
    kind = mtype[0] if mtype else ""
    try:
        if kind == "ADT":
            result = handle_adt(msg)
        elif kind in ("ORM", "OMI", "OMG"):
            result = handle_order(msg)
        elif kind == "ORU":
            result = handle_oru(msg)
        elif kind == "MDM":
            result = handle_mdm(msg)
        elif kind == "VXU":
            result = handle_vxu(msg)
        elif kind == "QBP":
            resp = handle_qbp(msg)
            messages.log(direction="in", protocol="hl7v2", message_type=msg.message_type,
                         status="ok", peer=peer, control_id=msg.control_id, payload=raw, response=resp)
            return resp
        else:
            raise Hl7Reject(f"unsupported message type {msg.message_type}")
    except Hl7Reject as e:
        ack = hl7v2.build_ack(msg, code="AR", text=str(e)[:80])
        messages.log(direction="in", protocol="hl7v2", message_type=msg.message_type, status="rejected",
                     peer=peer, control_id=msg.control_id, payload=raw, response=ack, error=str(e))
        return ack
    except (Hl7Error, ValueError, KeyError) as e:
        ack = hl7v2.build_ack(msg, code="AE", text=str(e)[:80])
        messages.log(direction="in", protocol="hl7v2", message_type=msg.message_type, status="error",
                     peer=peer, control_id=msg.control_id, payload=raw, response=ack, error=str(e))
        return ack
    except Exception as e:  # noqa: BLE001
        log.exception("HL7 processing failed")
        ack = hl7v2.build_ack(msg, code="AE", text="internal error")
        messages.log(direction="in", protocol="hl7v2", message_type=msg.message_type, status="error",
                     peer=peer, control_id=msg.control_id, payload=raw, response=ack, error=repr(e))
        return ack
    ack = hl7v2.build_ack(msg, code="AA", text="")
    messages.log(direction="in", protocol="hl7v2", message_type=msg.message_type, status="ok",
                 peer=peer, control_id=msg.control_id, person_id=result.get("person_id"),
                 payload=raw, response=ack)
    return ack
