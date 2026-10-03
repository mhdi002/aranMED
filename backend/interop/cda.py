"""HL7 C-CDA R2.1 Continuity of Care Document: export and import.

Export builds a CCD from the unified chart (US Realm header + CCD template
ids, LOINC-coded sections with a narrative table *and* coded entries) so it
renders in any CDA viewer and is machine-readable by receiving systems.

Import parses the same sections (problems, allergies, medications, results,
vital signs, immunizations, procedures, encounters) from a CCD produced by
any system, registers/matches the patient in the MPI, and writes the
entries as EHR rows attributed to the document's custodian. Narrative-only
documents are still kept whole as a DocumentReference.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any, Optional

from lxml import etree

from clinicaldb import facilities, mpi, settings
from ehr import chart as chart_mod, store

NS = "urn:hl7-org:v3"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
N = {"v3": NS}
LOINC_OID = "2.16.840.1.113883.6.1"
SNOMED_OID = "2.16.840.1.113883.6.96"
RXNORM_OID = "2.16.840.1.113883.6.88"
SYSTEM_OID = {"http://loinc.org": LOINC_OID, "http://snomed.info/sct": SNOMED_OID,
              "http://www.nlm.nih.gov/research/umls/rxnorm": RXNORM_OID}
OID_SYSTEM = {v: k for k, v in SYSTEM_OID.items()}

SECTIONS = {  # key: (templateId, LOINC, title, resource type)
    "allergies": ("2.16.840.1.113883.10.20.22.2.6.1", "48765-2", "Allergies and Intolerances", "allergy"),
    "medications": ("2.16.840.1.113883.10.20.22.2.1.1", "10160-0", "Medications", "medication"),
    "problems": ("2.16.840.1.113883.10.20.22.2.5.1", "11450-4", "Problem List", "condition"),
    "results": ("2.16.840.1.113883.10.20.22.2.3.1", "30954-2", "Results", "observation"),
    "vitals": ("2.16.840.1.113883.10.20.22.2.4.1", "8716-3", "Vital Signs", "observation"),
    "procedures": ("2.16.840.1.113883.10.20.22.2.7.1", "47519-4", "Procedures", "procedure"),
    "immunizations": ("2.16.840.1.113883.10.20.22.2.2.1", "11369-6", "Immunizations", "immunization"),
    "encounters": ("2.16.840.1.113883.10.20.22.2.22.1", "46240-8", "Encounters", "encounter"),
}
_BY_LOINC = {v[1]: k for k, v in SECTIONS.items()}


def _e(parent, tag: str, **attrs) -> etree._Element:
    el = etree.SubElement(parent, f"{{{NS}}}{tag}")
    for k, v in attrs.items():
        if v is not None:
            el.set(f"{{{XSI}}}{k[4:]}" if k.startswith("xsi_") else k, str(v))
    return el


def _ts(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    digits = "".join(ch for ch in str(v) if ch.isdigit())
    return digits[:14] or None


def _code(parent, tag: str, row: dict) -> etree._Element:
    sys_ = row.get("code_system")
    if row.get("code") and sys_ in SYSTEM_OID:
        el = _e(parent, tag, code=row["code"], codeSystem=SYSTEM_OID[sys_],
                displayName=row.get("display") or row.get("text"))
    else:
        el = _e(parent, tag, nullFlavor="OTH")
    ot = _e(el, "originalText")
    ot.text = row.get("display") or row.get("text") or ""
    return el


def _narrative(section, headers: list[str], rows: list[list[str]]) -> None:
    text = _e(section, "text")
    if not rows:
        p = _e(text, "paragraph")
        p.text = "No information recorded."
        return
    table = _e(text, "table", border="1", width="100%")
    tr = _e(_e(table, "thead"), "tr")
    for h in headers:
        _e(tr, "th").text = h
    tb = _e(table, "tbody")
    for r in rows:
        tr = _e(tb, "tr")
        for c in r:
            _e(tr, "td").text = c or ""


def export_ccd(person_id: str) -> bytes:
    c = chart_mod.build(person_id)
    if not c:
        raise ValueError("unknown person")
    p = c["person"]
    d = p["demographics"]
    loc = facilities.local()
    root = etree.Element(f"{{{NS}}}ClinicalDocument", nsmap={None: NS, "xsi": XSI})
    _e(root, "realmCode", code="US")
    _e(root, "typeId", root="2.16.840.1.113883.1.3", extension="POCD_HD000040")
    _e(root, "templateId", root="2.16.840.1.113883.10.20.22.1.1", extension="2015-08-01")
    _e(root, "templateId", root="2.16.840.1.113883.10.20.22.1.2", extension="2015-08-01")
    _e(root, "id", root=str(uuid.uuid4()))
    _e(root, "code", code="34133-9", codeSystem=LOINC_OID, displayName="Summarization of Episode Note")
    _e(root, "title").text = f"Continuity of Care Document — {p['name']}"
    now = time.strftime("%Y%m%d%H%M%S", time.gmtime()) + "+0000"
    _e(root, "effectiveTime", value=now)
    _e(root, "confidentialityCode", code="N", codeSystem="2.16.840.1.113883.5.25")
    _e(root, "languageCode", code=d.get("language") or "en-US")
    rt = _e(_e(root, "recordTarget"), "patientRole")
    for i in p["identifiers"]:
        root_oid = i["system"][8:] if i["system"].startswith("urn:oid:") else i["system"]
        _e(rt, "id", root=root_oid, extension=i["value"])
    if d.get("address"):
        _e(_e(rt, "addr"), "streetAddressLine").text = str(d["address"])
    if d.get("phone"):
        _e(rt, "telecom", value=f"tel:{d['phone']}")
    pat = _e(rt, "patient")
    nm = _e(pat, "name")
    for g in (d.get("given") or "").split():
        _e(nm, "given").text = g
    _e(nm, "family").text = d.get("family") or ""
    _e(pat, "administrativeGenderCode",
       code={"male": "M", "female": "F"}.get(d.get("sex") or "", "UN"),
       codeSystem="2.16.840.1.113883.5.1")
    if d.get("birth_date"):
        _e(pat, "birthTime", value=_ts(d["birth_date"]))
    author = _e(root, "author")
    _e(author, "time", value=now)
    aa = _e(author, "assignedAuthor")
    _e(aa, "id", root=loc["oid"])
    _e(_e(aa, "representedOrganization"), "name").text = loc["name"]
    cust = _e(_e(_e(root, "custodian"), "assignedCustodian"), "representedCustodianOrganization")
    _e(cust, "id", root=loc["oid"])
    _e(cust, "name").text = loc["name"]
    body = _e(_e(root, "component"), "structuredBody")

    s = c["sections"]
    obs = s.get("observation", [])
    data = {
        "allergies": s.get("allergy", []), "medications": s.get("medication", []),
        "problems": s.get("condition", []),
        "results": [o for o in obs if o.get("category") == "laboratory"],
        "vitals": [o for o in obs if o.get("category") == "vital-signs"],
        "procedures": s.get("procedure", []), "immunizations": s.get("immunization", []),
        "encounters": s.get("encounter", []),
    }
    for key, (tid, loinc, title, _rt) in SECTIONS.items():
        sec = _e(_e(body, "component"), "section")
        _e(sec, "templateId", root=tid, extension="2015-08-01")
        _e(sec, "code", code=loinc, codeSystem=LOINC_OID, displayName=title)
        _e(sec, "title").text = title
        items = data[key]
        rows = []
        for it in items:
            label = it.get("display") or it.get("text") or it.get("type_text") or ""
            if key in ("results", "vitals"):
                val = it.get("value_num") if it.get("value_num") is not None else it.get("value_text")
                rows.append([label, f"{val if val is not None else ''} {it.get('unit') or ''}".strip(),
                             it.get("interpretation") or "", it.get("effective") or ""])
            elif key == "medications":
                rows.append([label, " ".join(x for x in (it.get("dose"), it.get("route"), it.get("frequency")) if x),
                             it.get("status") or ""])
            elif key == "allergies":
                rows.append([label, it.get("reaction") or "", it.get("criticality") or it.get("severity") or ""])
            elif key == "encounters":
                rows.append([it.get("class") or "", it.get("reason") or label, it.get("start_at") or ""])
            else:
                rows.append([label, it.get("clinical_status") or it.get("status") or "",
                             it.get("onset") or it.get("performed") or it.get("occurrence") or ""])
        _narrative(sec, {"results": ["Test", "Value", "Flag", "Date"], "vitals": ["Vital", "Value", "Flag", "Date"],
                         "medications": ["Medication", "Dosage", "Status"],
                         "allergies": ["Substance", "Reaction", "Severity"],
                         "encounters": ["Class", "Reason", "Date"]}.get(key, ["Item", "Status", "Date"]), rows)
        for it in items:
            _entry(sec, key, it)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _entry(sec, key: str, it: dict) -> None:
    entry = _e(sec, "entry", typeCode="DRIV")
    src = f"{it.get('source_facility') or settings.facility_oid()}"
    if key == "medications":
        sa = _e(entry, "substanceAdministration", classCode="SBADM", moodCode="EVN")
        _e(sa, "id", root=src, extension=it.get("source_id") or it["id"])
        _e(sa, "statusCode", code="active" if (it.get("status") or "active") == "active" else "completed")
        if it.get("route"):
            _e(sa, "routeCode", nullFlavor="OTH").append(etree.fromstring(
                f'<originalText xmlns="{NS}">{_x(it["route"])}</originalText>'))
        if it.get("dose"):
            _e(sa, "doseQuantity", nullFlavor="OTH")
        mm = _e(_e(_e(sa, "consumable"), "manufacturedProduct", classCode="MANU"), "manufacturedMaterial")
        _code(mm, "code", it)
        if it.get("dose") or it.get("frequency"):
            txt = _e(sa, "text")
            txt.text = " ".join(x for x in (it.get("dose"), it.get("route"), it.get("frequency")) if x)
        return
    if key == "allergies":
        act = _e(entry, "act", classCode="ACT", moodCode="EVN")
        _e(act, "id", root=src, extension=it.get("source_id") or it["id"])
        _e(act, "code", code="CONC", codeSystem="2.16.840.1.113883.5.6")
        o = _e(_e(act, "entryRelationship", typeCode="SUBJ"), "observation", classCode="OBS", moodCode="EVN")
        _e(o, "code", code="ASSERTION", codeSystem="2.16.840.1.113883.5.4")
        _e(o, "value", xsi_type="CD", code="419199007", codeSystem=SNOMED_OID,
           displayName="Allergy to substance")
        pe = _e(_e(_e(o, "participant", typeCode="CSM"), "participantRole", classCode="MANU"),
                "playingEntity", classCode="MMAT")
        _code(pe, "code", it)
        if it.get("reaction"):
            r = _e(_e(o, "entryRelationship", typeCode="MFST", inversionInd="true"),
                   "observation", classCode="OBS", moodCode="EVN")
            _e(r, "code", code="ASSERTION", codeSystem="2.16.840.1.113883.5.4")
            v = _e(r, "value", xsi_type="CD", nullFlavor="OTH")
            _e(v, "originalText").text = it["reaction"]
        return
    if key == "encounters":
        enc = _e(entry, "encounter", classCode="ENC", moodCode="EVN")
        _e(enc, "id", root=src, extension=it.get("source_id") or it["id"])
        _e(enc, "code", code=it.get("class") or "AMB", codeSystem="2.16.840.1.113883.5.4")
        et = _e(enc, "effectiveTime")
        if it.get("start_at"):
            _e(et, "low", value=_ts(it["start_at"]))
        if it.get("reason"):
            o = _e(_e(enc, "entryRelationship", typeCode="RSON"), "observation", classCode="OBS", moodCode="EVN")
            _e(o, "code", code="404684003", codeSystem=SNOMED_OID)
            _e(_e(o, "value", xsi_type="CD", nullFlavor="OTH"), "originalText").text = it["reason"]
        return
    if key == "procedures":
        pr_ = _e(entry, "procedure", classCode="PROC", moodCode="EVN")
        _e(pr_, "id", root=src, extension=it.get("source_id") or it["id"])
        _code(pr_, "code", it)
        _e(pr_, "statusCode", code="completed")
        if it.get("performed"):
            _e(pr_, "effectiveTime", value=_ts(it["performed"]))
        return
    if key == "immunizations":
        sa = _e(entry, "substanceAdministration", classCode="SBADM", moodCode="EVN", negationInd="false")
        _e(sa, "id", root=src, extension=it.get("source_id") or it["id"])
        _e(sa, "statusCode", code="completed")
        if it.get("occurrence"):
            _e(sa, "effectiveTime", value=_ts(it["occurrence"]))
        mm = _e(_e(_e(sa, "consumable"), "manufacturedProduct", classCode="MANU"), "manufacturedMaterial")
        _code(mm, "code", it)
        return
    # problems / results / vitals: an observation
    o = _e(entry, "observation", classCode="OBS", moodCode="EVN")
    _e(o, "id", root=src, extension=it.get("source_id") or it["id"])
    if key == "problems":
        _e(o, "code", code="55607006", codeSystem=SNOMED_OID, displayName="Problem")
        _e(o, "statusCode", code="completed")
        et = _e(o, "effectiveTime")
        if it.get("onset"):
            _e(et, "low", value=_ts(it["onset"]))
        v = _code(o, "value", it)
        v.set(f"{{{XSI}}}type", "CD")
        return
    _code(o, "code", it)
    _e(o, "statusCode", code="completed")
    if it.get("effective"):
        _e(o, "effectiveTime", value=_ts(it["effective"]))
    if it.get("value_num") is not None:
        v = _e(o, "value", value=it["value_num"], unit=it.get("unit") or "1")
        v.set(f"{{{XSI}}}type", "PQ")
    elif it.get("value_text"):
        v = _e(o, "value")
        v.set(f"{{{XSI}}}type", "ST")
        v.text = it["value_text"]
    if it.get("interpretation"):
        _e(o, "interpretationCode", code=it["interpretation"], codeSystem="2.16.840.1.113883.5.83")
    if it.get("ref_low") is not None or it.get("ref_high") is not None:
        ivl = _e(_e(_e(o, "referenceRange"), "observationRange"), "value")
        ivl.set(f"{{{XSI}}}type", "IVL_PQ")
        if it.get("ref_low") is not None:
            _e(ivl, "low", value=it["ref_low"], unit=it.get("unit") or "1")
        if it.get("ref_high") is not None:
            _e(ivl, "high", value=it["ref_high"], unit=it.get("unit") or "1")


def _x(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- import
def _code_of(el) -> dict[str, Optional[str]]:
    if el is None:
        return {}
    ot = el.find("v3:originalText", N)
    text = (ot.text if ot is not None and ot.text else None) or el.get("displayName")
    sys_oid = el.get("codeSystem")
    return {"code": el.get("code"), "code_system": OID_SYSTEM.get(sys_oid, f"urn:oid:{sys_oid}" if sys_oid else None)
            if el.get("code") else None, "display": text, "text": text}


def _ts_iso(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    v = v[:14]
    if len(v) >= 12:
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}T{v[8:10]}:{v[10:12]}:{v[12:14] or '00'}"
    if len(v) >= 8:
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}"
    return v


def parse_ccd(xml: bytes) -> dict[str, Any]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    root = etree.fromstring(xml, parser)
    if etree.QName(root).localname != "ClinicalDocument":
        raise ValueError("not a CDA ClinicalDocument")
    role = root.find(".//v3:recordTarget/v3:patientRole", N)
    if role is None:
        raise ValueError("CDA has no recordTarget/patientRole")
    idents = []
    for i in role.findall("v3:id", N):
        r, ext = i.get("root"), i.get("extension")
        if r and ext:
            system = r if r.startswith(("urn:", "http")) else f"urn:oid:{r}"
            idents.append({"system": system, "value": ext,
                           "type": "NI" if system == settings.national_id_system() else "MR",
                           "facility_oid": r if not r.startswith(("urn:", "http")) else None})
    pat = role.find("v3:patient", N)
    name = pat.find("v3:name", N) if pat is not None else None
    demo = {"family": (name.findtext("v3:family", namespaces=N) if name is not None else None),
            "given": " ".join(g.text for g in (name.findall("v3:given", N) if name is not None else []) if g.text) or None,
            "sex": {"M": "male", "F": "female"}.get((pat.find("v3:administrativeGenderCode", N).get("code")
                                                     if pat is not None and pat.find("v3:administrativeGenderCode", N) is not None else ""), None),
            "birth_date": _ts_iso((pat.find("v3:birthTime", N).get("value") if pat is not None and
                                   pat.find("v3:birthTime", N) is not None else None))}
    cust = root.find(".//v3:custodian//v3:representedCustodianOrganization/v3:id", N)
    custodian = cust.get("root") if cust is not None else None
    items: dict[str, list[dict]] = {}
    for sec in root.findall(".//v3:structuredBody/v3:component/v3:section", N):
        code = sec.find("v3:code", N)
        key = _BY_LOINC.get(code.get("code") if code is not None else "")
        if not key:
            continue
        rtype = SECTIONS[key][3]
        for entry in sec.findall("v3:entry", N):
            it = _parse_entry(key, entry)
            if it:
                if key == "results":
                    it["category"] = "laboratory"
                elif key == "vitals":
                    it["category"] = "vital-signs"
                items.setdefault(rtype, []).append(it)
    return {"demographics": demo, "identifiers": idents, "custodian": custodian, "items": items,
            "title": root.findtext("v3:title", namespaces=N)}


def _id(el) -> tuple[Optional[str], Optional[str]]:
    i = el.find("v3:id", N) if el is not None else None
    return (i.get("root"), i.get("extension") or i.get("root")) if i is not None else (None, None)


def _parse_entry(key: str, entry) -> Optional[dict]:
    if key == "medications":
        sa = entry.find("v3:substanceAdministration", N)
        if sa is None:
            return None
        fac, sid = _id(sa)
        mm = sa.find(".//v3:manufacturedMaterial/v3:code", N)
        st = sa.find("v3:statusCode", N)
        return {**_code_of(mm), "source_facility": fac, "source_id": sid, "kind": "statement",
                "status": "active" if st is None or st.get("code") == "active" else "completed",
                "frequency": sa.findtext("v3:text", namespaces=N),
                "route": (sa.find("v3:routeCode/v3:originalText", N).text
                          if sa.find("v3:routeCode/v3:originalText", N) is not None else None)}
    if key == "allergies":
        act = entry.find("v3:act", N)
        if act is None:
            return None
        fac, sid = _id(act)
        pe = act.find(".//v3:playingEntity/v3:code", N)
        rx = act.find(".//v3:entryRelationship[@typeCode='MFST']//v3:value/v3:originalText", N)
        return {**_code_of(pe), "source_facility": fac, "source_id": sid, "status": "active",
                "reaction": rx.text if rx is not None else None}
    if key == "encounters":
        enc = entry.find("v3:encounter", N)
        if enc is None:
            return None
        fac, sid = _id(enc)
        low = enc.find("v3:effectiveTime/v3:low", N)
        reason = enc.find(".//v3:entryRelationship[@typeCode='RSON']//v3:value/v3:originalText", N)
        return {"source_facility": fac, "source_id": sid, "status": "finished",
                "class": (enc.find("v3:code", N).get("code") if enc.find("v3:code", N) is not None else "AMB"),
                "start_at": _ts_iso(low.get("value") if low is not None else None),
                "reason": reason.text if reason is not None else None}
    if key == "procedures":
        pr_ = entry.find("v3:procedure", N)
        if pr_ is None:
            return None
        fac, sid = _id(pr_)
        et = pr_.find("v3:effectiveTime", N)
        return {**_code_of(pr_.find("v3:code", N)), "source_facility": fac, "source_id": sid,
                "status": "completed", "performed": _ts_iso(et.get("value") if et is not None else None)}
    if key == "immunizations":
        sa = entry.find("v3:substanceAdministration", N)
        if sa is None:
            return None
        fac, sid = _id(sa)
        et = sa.find("v3:effectiveTime", N)
        return {**_code_of(sa.find(".//v3:manufacturedMaterial/v3:code", N)), "source_facility": fac,
                "source_id": sid, "status": "completed",
                "occurrence": _ts_iso(et.get("value") if et is not None else None)}
    o = entry.find("v3:observation", N)
    if o is None:
        return None
    fac, sid = _id(o)
    if key == "problems":
        low = o.find("v3:effectiveTime/v3:low", N)
        return {**_code_of(o.find("v3:value", N)), "source_facility": fac, "source_id": sid,
                "clinical_status": "active", "onset": _ts_iso(low.get("value") if low is not None else None)}
    v = o.find("v3:value", N)
    out = {**_code_of(o.find("v3:code", N)), "source_facility": fac, "source_id": sid, "status": "final"}
    if v is not None:
        t = v.get(f"{{{XSI}}}type")
        if t == "PQ" and v.get("value"):
            out["value_num"] = float(v.get("value"))
            out["unit"] = v.get("unit") if v.get("unit") != "1" else None
        else:
            out["value_text"] = v.text or v.get("value")
    et = o.find("v3:effectiveTime", N)
    out["effective"] = _ts_iso(et.get("value") if et is not None else None)
    ic = o.find("v3:interpretationCode", N)
    if ic is not None:
        out["interpretation"] = ic.get("code")
    lo = o.find(".//v3:referenceRange//v3:low", N)
    hi = o.find(".//v3:referenceRange//v3:high", N)
    if lo is not None and lo.get("value"):
        out["ref_low"] = float(lo.get("value"))
    if hi is not None and hi.get("value"):
        out["ref_high"] = float(hi.get("value"))
    return out


def import_ccd(xml: bytes, *, actor: Optional[str] = None,
               source_facility: Optional[str] = None) -> dict[str, Any]:
    doc = parse_ccd(xml)
    src = source_facility or doc["custodian"] or settings.facility_oid()
    reg = mpi.register_person(doc["demographics"], doc["identifiers"], source_facility=src)
    pid = reg["person_id"]
    counts: dict[str, int] = {}
    for rtype, items in doc["items"].items():
        for it in items:
            it = {k: v for k, v in it.items() if v is not None}
            it.setdefault("source_facility", src)
            store.create(rtype, {**it, "person_id": pid}, actor=actor)
            counts[rtype] = counts.get(rtype, 0) + 1
    store.create("document", {"person_id": pid, "source_facility": src,
                              "source_id": "cda:" + hashlib.sha256(xml).hexdigest()[:24], "doc_type": "cda",
                              "title": doc.get("title") or "Imported CCD",
                              "content_type": "application/xml",
                              "content": xml.decode("utf-8", errors="replace"),
                              "status": "current"}, actor=actor)
    return {"person_id": pid, "outcome": reg["outcome"], "imported": counts}
