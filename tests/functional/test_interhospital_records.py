"""Functional, multi-hospital: every record survives moving between hospitals.

Four independent AranMed hospitals run as separate processes, each with its
own database, PACS storage and DICOM listener:

* A (2.25.701), B (2.25.702) and C (2.25.703) use the default opt-out
  sharing policy.
* E (2.25.704) runs the opt-in policy.

What is proven here, end to end over the real peer protocols (FHIR, STOW-RS,
peer tokens):

1. A full record moves A -> B for a patient B has never seen. Every resource
   type and every field arrives, including medication dose, route,
   frequency, interval and the dose-change history. Encounter and result
   links still resolve. Documents decrypt. Images are byte-identical. The
   author stays A.
2. A transfer to a hospital that already knows the patient merges onto that
   person, with no duplicates.
3. Re-sending a package is genuinely idempotent, and a failed FHIR
   transaction never deletes rows that already existed.
4. Failure paths: reject, cancel, an unreachable receiver, a receiver that
   fails then recovers, an imaging-only failure, and invalid transitions on
   either side.
5. A chain A -> B -> C keeps the original authorship and the dose history.
6. A visiting patient (no transfer) is seen live: every type, document
   content on demand, timeline and imaging. After a later transfer nothing
   is shown twice.
7. Consent across hospitals covers both the record and the images:
   * a wildcard refusal;
   * the opt-in policy;
   * ETREAT, audited at both ends.
8. An MPI merge after a transfer carries the transferred record and images
   to the survivor.
9. The agent's clinical tools read and act across hospitals.
"""
from __future__ import annotations

import http.server
import os
import threading
import time
from typing import Any

import httpx
import pytest

from tests.functional.dicom_factory import make_study, to_bytes
from tests.functional.hospital import Hospital

NET_SECRET = "round3-network-secret-0123456789abcdef"
SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
A_OID, B_OID, C_OID, E_OID = "2.25.701", "2.25.702", "2.25.703", "2.25.704"


# --------------------------------------------------------------------------- network
@pytest.fixture(scope="module")
def net(tmp_path_factory):
    work = tmp_path_factory.mktemp("records")
    env = {"PEER_SECRET": NET_SECRET, "TRANSFER_RETRY_BASE_SEC": "0.2"}
    hs = {
        "A": Hospital("Isfahan University Hospital (A)", A_OID, work, ae_title="RECA", extra_env=env),
        "B": Hospital("Tabriz Medical Center (B)", B_OID, work, ae_title="RECB", extra_env=env),
        "C": Hospital("Mashhad Imam Reza (C)", C_OID, work, ae_title="RECC", extra_env=env),
        "E": Hospital("Opt-in Clinic (E)", E_OID, work, ae_title="RECE",
                      extra_env={**env, "CONSENT_SHARING_DEFAULT": "opt-in"}),
    }
    for h in hs.values():
        h.start()
    try:
        for x in hs.values():
            for y in hs.values():
                if x is not y:
                    ok(x.post("/api/facilities", json=y.facility_record(secret_env="PEER_SECRET")))
        yield hs
    finally:
        for h in hs.values():
            h.stop()


# --------------------------------------------------------------------------- helpers
def ok(r: httpx.Response, code: int = 200) -> Any:
    assert r.status_code == code, f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:500]}"
    return r.json() if r.content else {}


def register(h: Hospital, family: str, given: str, *, nat: str | None = None, mrn: str | None = None,
             birth: str = "1966-02-03", sex: str = "male") -> str:
    body: dict[str, Any] = {"demographics": {"family": family, "given": given, "birth_date": birth, "sex": sex}}
    if nat:
        body["national_id"] = nat
    if mrn:
        body["mrn"] = mrn
    return ok(h.post("/api/clinical/patients", role="doctor", json=body))["person_id"]


def add(h: Hospital, pid: str, kind: str, body: dict) -> dict:
    return ok(h.post(f"/api/clinical/patients/{pid}/{kind}", role="doctor", json=body))


def items(h: Hospital, pid: str, rtype: str) -> list[dict]:
    return ok(h.get(f"/api/clinical/patients/{pid}/{rtype}", role="doctor"))["items"]


def patch(h: Hospital, rtype: str, rid: str, changes: dict) -> dict:
    return ok(httpx.patch(f"{h.base}/api/clinical/resources/{rtype}/{rid}", headers=h.h("doctor"),
                          json={"changes": changes}, timeout=30))


def history(h: Hospital, rtype: str, rid: str) -> list[dict]:
    return ok(h.get(f"/api/clinical/resources/{rtype}/{rid}/history", role="doctor"))["versions"]


def chart(h: Hospital, pid: str, **params) -> dict:
    return ok(h.get(f"/api/clinical/patients/{pid}/chart", role="doctor",
                    params={k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items()}))


def stow(h: Hospital, dsets) -> None:
    from pacs import multipart
    bnd = multipart.boundary()
    body = multipart.encode(((to_bytes(d), "application/dicom") for d in dsets), bnd)
    ok(h.post("/api/dicom-web/studies", content=body,
              headers={"Content-Type": multipart.content_type(bnd, "application/dicom")}))


def wait(pred, timeout: float = 90, step: float = 0.4):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


def instances(h: Hospital, uid: str) -> list[tuple[str, str]]:
    out = ok(h.get(f"/api/dicom-web/studies/{uid}/instances"))
    return sorted((x["0020000E"]["Value"][0], x["00080018"]["Value"][0]) for x in out)


def instance_bytes(h: Hospital, uid: str, series: str, sop: str) -> bytes:
    r = h.get(f"/api/dicom-web/studies/{uid}/series/{series}/instances/{sop}",
              headers={"Accept": "application/dicom"})
    assert r.status_code == 200, r.text
    return r.content


def mint(issuer: str, audience: str, purpose: str = "TREAT", practitioner: str = "dr-test") -> str:
    """A peer token as hospital *issuer* would mint it (same shared secret)."""
    from clinicaldb import principal
    old = os.environ.get("FACILITY_OID")
    os.environ["FACILITY_OID"] = issuer
    try:
        return principal.mint_peer_token(audience_oid=audience, secret=NET_SECRET,
                                         practitioner=practitioner, purpose=purpose)
    finally:
        if old is None:
            os.environ.pop("FACILITY_OID", None)
        else:
            os.environ["FACILITY_OID"] = old


def transfer(src: Hospital, dst: Hospital, pid: str, **kw) -> tuple[dict, dict]:
    t = ok(src.post("/api/transfers", role="doctor", json={
        "person_id": pid, "to_facility": dst.oid, "urgency": kw.get("urgency", "urgent"),
        "reason": kw.get("reason", "Higher level of care"), "clinical_summary": kw.get("summary", "stable"),
        "transport_mode": "ALS ambulance", "include_imaging": kw.get("include_imaging", True)}))
    assert t["status"] == "requested" and t["remote_id"], t
    incoming = ok(dst.get("/api/transfers", role="doctor", params={"direction": "incoming"}))["transfers"]
    return t, next(x for x in incoming if x["remote_id"] == t["id"])


def accept_and_deliver(src: Hospital, dst: Hospital, ts: dict, td: dict,
                       expect_src: str = "delivered") -> tuple[dict, dict]:
    ok(dst.post(f"/api/transfers/{td['id']}/accept", role="doctor", json={"note": "bed ready"}))
    td = wait(lambda: (x := ok(dst.get(f"/api/transfers/{td['id']}", role="doctor")))["package_status"]
              in ("received", "failed") and x)
    assert td["package_status"] == "received", td
    ts = wait(lambda: (x := ok(src.get(f"/api/transfers/{ts['id']}", role="doctor")))["package_status"]
              in ("delivered", "partial", "failed") and x)
    assert ts["package_status"] == expect_src, ts
    return ts, td


def finish(src: Hospital, dst: Hospital, ts: dict, td: dict) -> None:
    ok(src.post(f"/api/transfers/{ts['id']}/depart", role="doctor", json={}))
    ok(dst.post(f"/api/transfers/{td['id']}/arrive", role="doctor", json={}))
    ok(dst.post(f"/api/transfers/{td['id']}/complete", role="doctor", json={}))
    assert ok(src.get(f"/api/transfers/{ts['id']}", role="doctor"))["status"] == "completed"


def norm(v: Any) -> Any:
    """Normalise representation-only differences (time zone suffix, int vs float)."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if len(s) > 10 and s[:4].isdigit() and s[4] == "-" and s[10] == " ":
            s = s[:10] + "T" + s[11:]
        return s[:-1] if s.endswith("Z") else s
    return v


FIELDS = {
    "encounter": ("class", "type_text", "reason", "start_at", "end_at", "location", "department",
                  "attending", "admit_source", "disposition", "priority", "status"),
    "condition": ("display", "code", "code_system", "clinical_status", "verification", "severity",
                  "onset", "abatement"),
    "allergy": ("display", "reaction", "severity", "criticality", "category", "onset", "status"),
    "medication": ("display", "kind", "dose", "route", "frequency", "frequency_hours", "status",
                   "start_at", "end_at", "prescriber", "indication"),
    "observation": ("display", "code", "code_system", "category", "value_num", "value_text", "unit",
                    "ref_low", "ref_high", "interpretation", "effective", "performer", "status"),
    "procedure": ("display", "code", "code_system", "performed", "performer", "body_site", "outcome", "status"),
    "immunization": ("display", "occurrence", "lot", "dose_number", "site", "route", "performer", "status"),
    "document": ("doc_type", "title", "author", "content_type", "effective", "status"),
    "service_request": ("display", "category", "priority", "reason", "requester", "occurrence",
                        "accession", "status", "intent", "data"),
    "diagnostic_report": ("display", "category", "conclusion", "effective", "issued", "performer",
                          "study_uid", "status"),
}
LABEL = {"document": "title", "encounter": "type_text"}


def label_of(rtype: str, row: dict) -> Any:
    return row.get(LABEL.get(rtype, "display"))


def key_of(rtype: str, row: dict) -> Any:
    k = LABEL.get(rtype, "display")
    return (row.get(k), row.get("effective") or row.get("start_at") or "") if rtype == "observation" else row.get(k)


def assert_same_record(src: Hospital, src_pid: str, dst: Hospital, dst_pid: str, author: str) -> dict:
    """Every row at *src* exists at *dst* with equal clinical fields and the
    original author. Returns {rtype: {key: (src_row, dst_row)}}."""
    pairs: dict[str, dict] = {}
    for rtype, fields in FIELDS.items():
        have = {key_of(rtype, r): r for r in items(dst, dst_pid, rtype) if r["source_facility"] == author}
        for r in items(src, src_pid, rtype):
            if r["source_facility"] != author:
                continue
            k = key_of(rtype, r)
            assert k in have, f"{rtype} {k!r} missing at {dst.name}; has {list(have)}"
            got = have[k]
            for f in fields:
                if r.get(f) not in (None, "", {}, []):
                    assert norm(got.get(f)) == norm(r[f]), (rtype, k, f, r[f], got.get(f))
            assert got["source_facility"] == author and got["source_id"] == r["source_id"]
            pairs.setdefault(rtype, {})[k] = (r, got)
    return pairs


def seed_full_record(a: Hospital, pid: str, mrn: str) -> dict:
    """Everything a hospital can hold about a patient, with two dose changes."""
    out: dict[str, Any] = {}
    amb = add(a, pid, "encounters", {
        "class": "AMB", "type_text": "Cardiology clinic visit", "reason": "Exertional angina",
        "start_at": "2026-03-01T09:30:00", "end_at": "2026-03-01T10:05:00", "location": "Clinic 4",
        "department": "Cardiology", "attending": "Dr. Rostami", "status": "finished"})
    imp = add(a, pid, "encounters", {
        "class": "IMP", "type_text": "Admission for PCI", "reason": "NSTEMI",
        "start_at": "2026-04-10T22:00:00", "end_at": "2026-04-14T12:00:00", "location": "Ward 3",
        "department": "CCU", "attending": "Dr. Kazemi", "admit_source": "emergency",
        "disposition": "home", "priority": "urgent", "status": "finished"})
    add(a, pid, "conditions", {"display": "Coronary artery disease", "code_system": SNOMED, "code": "53741008",
                               "clinical_status": "active", "verification": "confirmed", "severity": "moderate",
                               "onset": "2025-01-01", "encounter_id": amb["id"]})
    add(a, pid, "conditions", {"display": "Type 2 diabetes mellitus", "code_system": SNOMED, "code": "44054006",
                               "clinical_status": "active", "onset": "2015-06-01"})
    add(a, pid, "allergies", {"display": "Iodinated contrast", "reaction": "Anaphylaxis", "severity": "severe",
                              "criticality": "high", "category": "medication", "onset": "2019-02-02"})
    metformin = add(a, pid, "medications", {
        "display": "Metformin", "kind": "statement", "dose": "500 mg", "route": "PO",
        "frequency": "twice daily", "frequency_hours": 12, "status": "active", "start_at": "2015-06-01",
        "prescriber": "Dr. Amini", "indication": "Type 2 diabetes"})
    patch(a, "medication", metformin["id"], {"dose": "1000 mg"})
    patch(a, "medication", metformin["id"], {"frequency": "three times daily", "frequency_hours": 8})
    add(a, pid, "medications", {
        "display": "Clopidogrel", "kind": "request", "dose": "75 mg", "route": "PO", "frequency": "daily",
        "frequency_hours": 24, "status": "active", "start_at": "2026-04-14", "end_at": "2027-04-14",
        "prescriber": "Dr. Kazemi", "indication": "after PCI"})
    add(a, pid, "medications", {"display": "Atorvastatin", "kind": "statement", "dose": "40 mg", "route": "PO",
                                "frequency": "at night", "frequency_hours": 24, "status": "active"})
    creat = [add(a, pid, "observations", {
        "category": "laboratory", "code_system": LOINC, "code": "2160-0", "display": "Creatinine",
        "value_num": v, "unit": "mg/dL", "ref_low": 0.6, "ref_high": 1.2, "effective": d,
        "performer": "Central Lab", "status": "final", "encounter_id": imp["id"]})
        for d, v in (("2026-04-11", 1.1), ("2026-04-12", 1.6), ("2026-04-13", 1.9))]
    add(a, pid, "observations", {"category": "vital-signs", "code_system": LOINC, "code": "85354-9",
                                 "display": "Blood pressure", "value_text": "128/76", "effective": "2026-04-13",
                                 "status": "final"})
    add(a, pid, "observations", {"category": "vital-signs", "code_system": LOINC, "code": "8867-4",
                                 "display": "Heart rate", "value_num": 72, "unit": "/min",
                                 "effective": "2026-04-13", "status": "final"})
    add(a, pid, "procedures", {"display": "Percutaneous coronary intervention", "code_system": SNOMED,
                               "code": "415070008", "performed": "2026-04-11", "performer": "Dr. Kazemi",
                               "body_site": "LAD", "outcome": "successful", "status": "completed"})
    add(a, pid, "immunizations", {"display": "Influenza vaccine", "occurrence": "2025-10-01",
                                  "lot": "FLU-2025-77", "dose_number": "1", "site": "left deltoid",
                                  "route": "IM", "performer": "Nurse Rahimi", "status": "completed"})
    add(a, pid, "documents", {"doc_type": "discharge-summary", "title": "Discharge after PCI",
                              "content": "Discharged on DAPT. Follow up in 4 weeks.\nخلاصه ترخیص: حال عمومی خوب.",
                              "effective": "2026-04-14", "author": "Dr. Kazemi", "content_type": "text/plain",
                              "encounter_id": imp["id"]})
    add(a, pid, "service-requests", {"category": "imaging", "display": "Cardiac MRI viability",
                                     "status": "active", "priority": "routine", "reason": "viability",
                                     "requester": "Dr. Kazemi", "occurrence": "2026-05-01T09:00:00",
                                     "data": {"modality": "MR"}})
    add(a, pid, "diagnostic-reports", {"category": "LAB", "display": "Renal panel",
                                       "conclusion": "Rising creatinine after contrast",
                                       "effective": "2026-04-13", "issued": "2026-04-13T10:00:00Z",
                                       "performer": "Central Lab", "status": "final",
                                       "result_ids": [o["id"] for o in creat]})
    add(a, pid, "consents", {"category": "permit-sharing", "grantee": "*", "status": "active",
                             "text": "Shares with all network hospitals"})
    ct_uid, ct = make_study(n_series=2, per_series=3, patient_name="Ahmadi^Dariush", patient_id=mrn,
                            issuer=a.oid, birth_date="19660203", study_desc="CT CORONARY ANGIO",
                            study_date="20260411")
    mr_uid, mr = make_study(n_series=1, per_series=2, modality="MR", patient_name="Ahmadi^Dariush",
                            patient_id=mrn, issuer=a.oid, study_desc="CARDIAC MRI", study_date="20260420")
    stow(a, ct)
    stow(a, mr)
    add(a, pid, "diagnostic-reports", {"category": "RAD", "display": "CT coronary angiography report",
                                       "conclusion": "Patent LAD stent", "effective": "2026-04-11",
                                       "study_uid": ct_uid, "status": "final", "performer": "Dr. Mousavi"})
    out.update(ct=ct_uid, mr=mr_uid, metformin=metformin["id"])
    return out


# --------------------------------------------------------------------------- 1. full record
NAT_P1 = "1111111111"


def test_full_record_transfer_to_new_hospital(net):
    a, b = net["A"], net["B"]
    pa = register(a, "Ahmadi", "Dariush", nat=NAT_P1, mrn="A-1001")
    seeded = seed_full_record(a, pa, "A-1001")
    assert not ok(b.get("/api/mpi/search", role="doctor", params={"identifier": NAT_P1}))["results"]

    ts, td = transfer(a, b, pa, reason="Cardiac surgery evaluation")
    pb = td["person_id"]
    ts, td = accept_and_deliver(a, b, ts, td)
    assert td["package_manifest"]["imported"]["MedicationStatement"] == 2
    assert td["package_manifest"]["imported"]["MedicationRequest"] == 1

    # The patient now exists at B (one person) with A's identifiers.
    hits = ok(b.get("/api/mpi/search", role="doctor", params={"identifier": NAT_P1}))["results"]
    assert [h["person_id"] for h in hits] == [pb]
    idents = {(i["system"], i["value"]) for i in ok(b.get(f"/api/mpi/{pb}", role="doctor"))["identifiers"]}
    assert (f"urn:oid:{A_OID}", "A-1001") in idents
    assert any(v == NAT_P1 for _, v in idents)

    # Every row and every field, authored by A.
    pairs = assert_same_record(a, pa, b, pb, author=A_OID)
    assert set(pairs) == set(FIELDS), set(FIELDS) - set(pairs)
    assert len(pairs["observation"]) == 5 and len(pairs["medication"]) == 3

    # Medication: structured dose survived, and the full dose history with it.
    _, met_b = pairs["medication"]["Metformin"]
    assert (met_b["dose"], met_b["route"], met_b["frequency"], float(met_b["frequency_hours"])) == \
        ("1000 mg", "PO", "three times daily", 8.0)
    hist_a = [(v["dose"], float(v["frequency_hours"])) for v in history(a, "medication", seeded["metformin"])]
    hist_b = [(v["dose"], float(v["frequency_hours"])) for v in history(b, "medication", met_b["id"])]
    assert hist_a == hist_b == [("500 mg", 12.0), ("1000 mg", 12.0), ("1000 mg", 8.0)]

    # Links between records resolve to B's own copies.
    enc_b = {e["type_text"]: e["id"] for e in items(b, pb, "encounters")}
    assert pairs["condition"]["Coronary artery disease"][1]["encounter_id"] == enc_b["Cardiology clinic visit"]
    assert pairs["document"]["Discharge after PCI"][1]["encounter_id"] == enc_b["Admission for PCI"]
    creat_b = {o["id"] for o in items(b, pb, "observations") if o["display"] == "Creatinine"}
    for row in pairs["observation"].values():
        if row[1]["display"] == "Creatinine":
            assert row[1]["encounter_id"] == enc_b["Admission for PCI"]
    renal = pairs["diagnostic_report"]["Renal panel"][1]
    assert set(renal["result_ids"]) == creat_b and len(creat_b) == 3

    # The document content is encrypted at rest at B and decrypts intact.
    doc_b = pairs["document"]["Discharge after PCI"][1]
    content = ok(b.get(f"/api/clinical/documents/{doc_b['id']}", role="doctor"))["content"]
    assert content == "Discharged on DAPT. Follow up in 4 weeks.\nخلاصه ترخیص: حال عمومی خوب."
    summary = next(d for d in items(b, pb, "documents") if d["doc_type"] == "transfer-summary")
    text = ok(b.get(f"/api/clinical/documents/{summary['id']}", role="doctor"))["content"]
    assert "Metformin" in text and "Iodinated contrast" in text

    # Consents are local legal records: not copied.
    assert not [c for c in items(b, pb, "consents") if c["source_facility"] == A_OID]
    # A's pending imaging order is history at B, not an order on B's worklist.
    wl = ok(b.get("/api/pacs/worklist", role="doctor"))
    assert not any("Cardiac MRI" in (w.get("procedure_description") or "") for w in wl.get("items", wl.get("worklist", [])))

    # Images: same series, same instances, byte-identical files, linked to B's person.
    for uid in (seeded["ct"], seeded["mr"]):
        assert instances(b, uid) == instances(a, uid)
        for series, sop in instances(a, uid):
            assert instance_bytes(b, uid, series, sop) == instance_bytes(a, uid, series, sop)
        v = ok(b.get(f"/api/pacs/studies/{uid}/verify"))
        assert v["ok"] == len(instances(a, uid)) and not v["corrupt"] and not v["missing"]
    ch = chart(b, pb)
    img = {s["StudyInstanceUID"]: s for s in ch["imaging"]}
    assert {seeded["ct"], seeded["mr"]} <= set(img)
    assert img[seeded["ct"]]["source"]["facility_oid"] == A_OID

    # B's history (timeline) shows A's visits, results, report and imaging.
    events = ok(b.get(f"/api/clinical/patients/{pb}/timeline", role="doctor"))["events"]
    labels = {(e["kind"], e["label"]) for e in events}
    assert ("encounter", "Admission for PCI") in labels
    assert ("imaging", "CT CORONARY ANGIO") in labels and ("imaging", "CARDIAC MRI") in labels
    assert any(k == "observation" and lab.startswith("Creatinine: 1.9") for k, lab in labels)
    assert all((e.get("source") or {}).get("facility_oid") == A_OID for e in events
               if e["kind"] in ("encounter", "procedure", "immunization"))
    assert events == sorted(events, key=lambda e: e["date"] or "", reverse=True)
    finish(a, b, ts, td)


# --------------------------------------------------------------------------- 2+3. existing, idempotent
NAT_P2 = "2222222222"


def test_existing_patient_merges_and_resend_is_idempotent(net):
    a, b = net["A"], net["B"]
    pa = register(a, "Karimi", "Shirin", nat=NAT_P2, mrn="A-2002", sex="female", birth="1980-08-08")
    add(a, pa, "allergies", {"display": "Penicillin", "reaction": "Urticaria"})
    add(a, pa, "medications", {"display": "Levothyroxine", "dose": "100 mcg", "route": "PO",
                               "frequency": "every morning", "frequency_hours": 24, "status": "active"})
    uid, ds = make_study(n_series=1, per_series=2, patient_name="Karimi^Shirin", patient_id="A-2002",
                         issuer=a.oid, sex="F", study_desc="NECK US", modality="CT")
    stow(a, ds)
    # B already knows her (national id) and holds its own record.
    pb = register(b, "Karimi", "Shirin", nat=NAT_P2, mrn="B-55", sex="female", birth="1980-08-08")
    add(b, pb, "conditions", {"display": "Hypothyroidism", "clinical_status": "active"})

    ts, td = transfer(a, b, pa)
    assert td["person_id"] == pb
    ts, td = accept_and_deliver(a, b, ts, td)
    assert [h["person_id"] for h in ok(b.get("/api/mpi/search", role="doctor",
                                               params={"identifier": NAT_P2}))["results"]] == [pb]
    ch = chart(b, pb)
    assert {c["display"] for c in ch["sections"]["condition"]} == {"Hypothyroidism"}
    assert [m["dose"] for m in ch["sections"]["medication"]] == ["100 mcg"]

    def counts():
        c = chart(b, pb)
        return {k: len(v) for k, v in c["sections"].items()}, len(instances(b, uid))
    before = counts()
    # Re-send while B is still expecting packages (accepted, then arrived): nothing duplicates.
    ok(a.post(f"/api/transfers/{ts['id']}/resend-package", role="doctor"))
    assert counts() == before
    ok(a.post(f"/api/transfers/{ts['id']}/depart", role="doctor", json={}))
    ok(b.post(f"/api/transfers/{td['id']}/arrive", role="doctor", json={}))
    ok(a.post(f"/api/transfers/{ts['id']}/resend-package", role="doctor"))
    assert counts() == before
    assert len(instances(b, uid)) == 2
    ok(b.post(f"/api/transfers/{td['id']}/complete", role="doctor", json={}))
    # After completion B no longer accepts packages for this transfer.
    r = a.post(f"/api/transfers/{ts['id']}/resend-package", role="doctor")
    assert r.status_code == 502 and "not expected" in r.text
    assert counts() == before

    # A failed transaction never deletes a row that already existed.
    allergy = next(x for x in items(b, pb, "allergies") if x["display"] == "Penicillin")
    bad_bundle = {"resourceType": "Bundle", "type": "transaction", "entry": [
        {"resource": {"resourceType": "AllergyIntolerance", "patient": {"reference": f"Patient/{pb}"},
                      "meta": {"source": f"urn:oid:{A_OID}#{allergy['source_id']}"},
                      "code": {"text": "Penicillin"}}, "request": {"method": "POST", "url": "AllergyIntolerance"}},
        {"resource": {"resourceType": "Condition", "subject": {"reference": "Patient/does-not-exist"},
                      "code": {"text": "x"}}, "request": {"method": "POST", "url": "Condition"}}]}
    r = b.post("/api/fhir/r4", json=bad_bundle, headers={"Content-Type": "application/fhir+json"})
    assert r.status_code >= 400
    assert any(x["id"] == allergy["id"] for x in items(b, pb, "allergies"))


# --------------------------------------------------------------------------- 4. failures
class _FlakyProxy(http.server.ThreadingHTTPServer):
    """Fails the first *failures* requests with 503, then forwards to *target*."""

    def __init__(self, target: str, failures: int):
        self.target, self.failures, self.seen = target.rstrip("/"), failures, []
        super().__init__(("127.0.0.1", 0), _ProxyHandler)


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _go(self):
        srv: _FlakyProxy = self.server  # type: ignore[assignment]
        srv.seen.append(self.path)
        if srv.failures > 0:
            srv.failures -= 1
            self.send_response(503)
            self.end_headers()
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        r = httpx.request(self.command, srv.target + self.path, content=body, timeout=60,
                          headers={k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")})
        self.send_response(r.status_code)
        for k, v in r.headers.items():
            if k.lower() not in ("content-length", "transfer-encoding", "connection", "content-encoding"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(r.content)))
        self.end_headers()
        self.wfile.write(r.content)

    do_GET = do_POST = do_PUT = _go


def test_transfer_failure_paths(net):
    a, b = net["A"], net["B"]
    # Reject: the reason reaches the sender.
    p = register(a, "Rejected", "Reza", mrn="A-3001")
    ts, td = transfer(a, b, p)
    ok(b.post(f"/api/transfers/{td['id']}/reject", role="doctor", json={"note": "No ICU bed"}))
    ta = ok(a.get(f"/api/transfers/{ts['id']}", role="doctor"))
    assert ta["status"] == "rejected" and ta["rejection_reason"] == "No ICU bed"
    assert b.post(f"/api/transfers/{td['id']}/accept", role="doctor", json={}).status_code == 409

    # Cancel by the sender: the receiver follows.
    p = register(a, "Cancelled", "Cyrus", mrn="A-3002")
    ts, td = transfer(a, b, p)
    ok(a.post(f"/api/transfers/{ts['id']}/cancel", role="doctor", json={"note": "Patient improved"}))
    assert ok(b.get(f"/api/transfers/{td['id']}", role="doctor"))["status"] == "cancelled"

    # Invalid steps are refused on both sides.
    p = register(a, "Steps", "Sara", mrn="A-3003", sex="female")
    ts, td = transfer(a, b, p)
    assert a.post(f"/api/transfers/{ts['id']}/arrive", role="doctor", json={}).status_code == 409
    assert b.post(f"/api/transfers/{td['id']}/depart", role="doctor", json={}).status_code == 409
    assert b.post(f"/api/transfers/{td['id']}/complete", role="doctor", json={}).status_code == 409
    # A peer pushing an impossible status is refused too.
    r = httpx.post(f"{b.base}/api/transfers/inbound/{ts['id']}/status", timeout=30,
                   headers={"Authorization": f"Bearer {mint(A_OID, B_OID, 'TRANSFER')}"},
                   json={"status": "completed", "by": "intruder"})
    assert r.status_code in (400, 409), r.text
    assert ok(b.get(f"/api/transfers/{td['id']}", role="doctor"))["status"] == "requested"
    ok(a.post(f"/api/transfers/{ts['id']}/cancel", role="doctor", json={}))

    # Receiver unreachable: the request fails cleanly and is recorded as cancelled.
    ok(a.post("/api/facilities", json={"oid": "2.25.799", "name": "Offline Hospital", "kind": "hospital",
                                       "base_url": "http://127.0.0.1:9", "fhir_base": "http://127.0.0.1:9/fhir",
                                       "secret_env": "PEER_SECRET", "trust_level": "peer"}))
    p = register(a, "Offline", "Omid", mrn="A-3004")
    r = a.post("/api/transfers", role="doctor", json={"person_id": p, "to_facility": "2.25.799",
                                                      "reason": "x", "include_imaging": False})
    assert r.status_code == 400 and "could not reach" in r.text
    out = ok(a.get("/api/transfers", role="doctor", params={"direction": "outgoing"}))["transfers"]
    dead = next(x for x in out if x["to_facility"] == "2.25.799")
    assert dead["status"] == "cancelled" and "delivery failed" in str(dead["history"])

    # Receiver briefly failing (5xx) then recovering: retries succeed.
    proxy = _FlakyProxy(b.base, failures=2)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        rec = b.facility_record(secret_env="PEER_SECRET")
        ok(a.post("/api/facilities", json={**rec, "base_url": f"http://127.0.0.1:{proxy.server_address[1]}"}))
        p = register(a, "Retry", "Roya", mrn="A-3005", sex="female")
        ts, td = transfer(a, b, p, include_imaging=False)
        assert sum(1 for x in proxy.seen if x.endswith("/inbound")) >= 3  # 2 failures + success
        assert td["status"] == "requested"
        ok(a.post(f"/api/transfers/{ts['id']}/cancel", role="doctor", json={}))
    finally:
        proxy.shutdown()
        ok(a.post("/api/facilities", json=b.facility_record(secret_env="PEER_SECRET")))

    # Imaging cannot be delivered: the record still arrives; the sender says "partial".
    rec = b.facility_record(secret_env="PEER_SECRET")
    ok(a.post("/api/facilities", json={**rec, "dicomweb_base": "http://127.0.0.1:9/dicom-web"}))
    try:
        p = register(a, "Partial", "Parsa", mrn="A-3006")
        add(a, p, "allergies", {"display": "Latex"})
        uid, ds = make_study(n_series=1, per_series=1, patient_name="Partial^Parsa", patient_id="A-3006",
                             issuer=a.oid)
        stow(a, ds)
        ts, td = transfer(a, b, p)
        ts, td = accept_and_deliver(a, b, ts, td, expect_src="partial")
        assert any(x["display"] == "Latex" for x in items(b, td["person_id"], "allergies"))
        bad = next(s for s in ts["package_manifest"]["studies"] if s["study_uid"] == uid)
        assert bad["status"] != "done" and bad["error"]
    finally:
        ok(a.post("/api/facilities", json=b.facility_record(secret_env="PEER_SECRET")))


# --------------------------------------------------------------------------- 5. chain
def test_chain_a_to_b_to_c_keeps_authorship(net):
    a, b, c = net["A"], net["B"], net["C"]
    pa = ok(a.get("/api/mpi/search", role="doctor", params={"identifier": NAT_P1}))["results"][0]["person_id"]
    pb = ok(b.get("/api/mpi/search", role="doctor", params={"identifier": NAT_P1}))["results"][0]["person_id"]
    add(b, pb, "observations", {"category": "laboratory", "code_system": LOINC, "code": "2160-0",
                                "display": "Creatinine", "value_num": 1.4, "unit": "mg/dL",
                                "effective": "2026-10-01", "ref_low": 0.6, "ref_high": 1.2})
    met_b = next(m for m in items(b, pb, "medications") if m["display"] == "Metformin")
    patch(b, "medication", met_b["id"], {"dose": "850 mg"})        # B changes the dose
    ts, tc = transfer(b, c, pb, reason="Transplant work-up")
    pc = tc["person_id"]
    accept_and_deliver(b, c, ts, tc)

    # A's records arrive at C still authored by A (as B holds them now, including
    # B's dose change); B's own records arrive authored by B.
    assert_same_record(b, pb, c, pc, author=A_OID)
    b_lab = [o for o in items(c, pc, "observations") if o["effective"].startswith("2026-10-01")]
    assert len(b_lab) == 1 and b_lab[0]["source_facility"] == B_OID and b_lab[0]["value_num"] == 1.4
    met_c = next(m for m in items(c, pc, "medications") if m["display"] == "Metformin")
    assert met_c["source_facility"] == A_OID and met_c["dose"] == "850 mg"
    assert [v["dose"] for v in history(c, "medication", met_c["id"])] == ["500 mg", "1000 mg", "1000 mg", "850 mg"]
    names = chart(c, pc)
    srcs = {i["source"]["facility"] for sec in names["sections"].values() for i in sec}
    assert {"Isfahan University Hospital (A)", "Tabriz Medical Center (B)"} <= srcs
    summaries = [d for d in items(c, pc, "documents") if d["doc_type"] == "transfer-summary"]
    assert {d["source_facility"] for d in summaries} == {A_OID, B_OID}
    # Images taken at A reached C through B.
    uids = {s["StudyInstanceUID"] for s in names["imaging"]}
    a_uids = {s["StudyInstanceUID"] for s in chart(a, pa)["imaging"]}
    assert a_uids <= uids


# --------------------------------------------------------------------------- 6. visiting patient
NAT_P3 = "3333333333"


def test_visiting_patient_sees_every_type_live_then_no_duplicates_after_transfer(net):
    a, c = net["A"], net["C"]
    pa = register(a, "Visitor", "Vahid", nat=NAT_P3, mrn="A-6001")
    add(a, pa, "encounters", {"class": "AMB", "type_text": "Nephrology clinic", "start_at": "2026-06-01T10:00:00",
                              "department": "Nephrology", "attending": "Dr. Farhadi"})
    add(a, pa, "conditions", {"display": "Chronic kidney disease stage 3", "clinical_status": "active"})
    add(a, pa, "allergies", {"display": "Sulfonamides", "reaction": "Rash", "criticality": "low"})
    add(a, pa, "medications", {"display": "Amlodipine", "dose": "5 mg", "route": "PO", "frequency": "daily",
                               "frequency_hours": 24, "status": "active"})
    add(a, pa, "observations", {"category": "laboratory", "display": "eGFR", "value_num": 48, "unit": "mL/min/1.73m2",
                                "effective": "2026-06-01", "ref_low": 60})
    add(a, pa, "procedures", {"display": "Renal ultrasound", "performed": "2026-06-01"})
    add(a, pa, "immunizations", {"display": "Hepatitis B vaccine", "occurrence": "2026-01-10", "lot": "HB-9"})
    add(a, pa, "documents", {"doc_type": "progress-note", "title": "Nephrology note",
                             "content": "CKD stable. Avoid NSAIDs. پرهیز از داروهای ضدالتهاب.",
                             "effective": "2026-06-01"})
    uid, ds = make_study(n_series=1, per_series=2, patient_name="Visitor^Vahid", patient_id="A-6001",
                         issuer=a.oid, study_desc="CT KIDNEYS", study_date="20260602")
    stow(a, ds)

    pc = register(c, "Visitor", "Vahid", nat=NAT_P3, mrn="C-9")
    live = chart(c, pc, include_remote=True)
    assert live["errors"] == [] and A_OID in live["facilities"]
    sec = live["sections"]
    expect = {"encounter": "Nephrology clinic", "condition": "Chronic kidney disease stage 3",
              "allergy": "Sulfonamides", "medication": "Amlodipine", "observation": "eGFR",
              "procedure": "Renal ultrasound", "immunization": "Hepatitis B vaccine", "document": "Nephrology note"}
    for rtype, label in expect.items():
        row = next(x for x in sec[rtype] if label_of(rtype, x) == label)
        assert row["source"]["held"] == "remote" and row["source"]["facility_oid"] == A_OID, rtype
    med = next(x for x in sec["medication"] if x["display"] == "Amlodipine")
    assert (med["dose"], med["route"], med["frequency"], float(med["frequency_hours"])) == ("5 mg", "PO", "daily", 24.0)
    egfr = next(x for x in sec["observation"] if x["display"] == "eGFR")
    assert egfr["value_num"] == 48 and egfr["unit"] == "mL/min/1.73m2" and egfr["interpretation"] == "L"
    enc = next(x for x in sec["encounter"] if x["type_text"] == "Nephrology clinic")
    assert enc["department"] == "Nephrology" and enc["attending"] == "Dr. Farhadi"
    # Document content on demand from A (A applies its consent and audits it).
    doc = next(x for x in sec["document"] if x["title"] == "Nephrology note")
    assert "content" not in doc
    full = ok(c.get(f"/api/clinical/patients/{pc}/remote-documents/{doc['source']['via']}/{doc['remote_id']}",
                    role="doctor"))
    assert full["content"] == "CKD stable. Avoid NSAIDs. پرهیز از داروهای ضدالتهاب."
    assert c.get(f"/api/clinical/patients/{pc}/remote-documents/{A_OID}/not-a-doc", role="doctor").status_code == 404
    # Timeline with other hospitals, including A's imaging.
    tl = ok(c.get(f"/api/clinical/patients/{pc}/timeline", role="doctor", params={"include_remote": "true"}))
    kinds = {(e["kind"], e["label"]) for e in tl["events"]}
    assert ("imaging", "CT KIDNEYS") in kinds and ("procedure", "Renal ultrasound") in kinds
    assert next(e for e in tl["events"] if e["kind"] == "imaging")["source"]["facility_oid"] == A_OID
    # Pull A's images on demand: byte-identical.
    job = ok(c.post("/api/pacs/retrieve", role="doctor", json={"study_uid": uid, "node_id": f"facility:{A_OID}",
                                                               "wait": True}))
    assert job["status"] == "done"
    for series, sop in instances(a, uid):
        assert instance_bytes(c, uid, series, sop) == instance_bytes(a, uid, series, sop)

    # Later the patient is transferred: the live view shows nothing twice.
    ts, tc = transfer(a, c, pa)
    assert tc["person_id"] == pc
    accept_and_deliver(a, c, ts, tc)
    again = chart(c, pc, include_remote=True)
    for rtype, label in expect.items():
        same = [x for x in again["sections"][rtype] if label_of(rtype, x) == label]
        assert len(same) == 1 and same[0]["source"]["held"] == "local", (rtype, same)
    assert [s["StudyInstanceUID"] for s in again["imaging"]].count(uid) == 1


# --------------------------------------------------------------------------- 7. consent
def test_consent_governs_record_and_images_across_hospitals(net):
    a, b, c, e = net["A"], net["B"], net["C"], net["E"]
    nat = "5555555555"
    pa = register(a, "Private", "Parisa", nat=nat, mrn="A-7001", sex="female")
    add(a, pa, "allergies", {"display": "Morphine", "reaction": "Respiratory depression"})
    uid, ds = make_study(n_series=1, per_series=1, patient_name="Private^Parisa", patient_id="A-7001",
                         issuer=a.oid, sex="F", study_desc="MRI PELVIS", modality="MR")
    stow(a, ds)
    pc = register(c, "Private", "Parisa", nat=nat, mrn="C-77", sex="female")
    assert any(x["display"] == "Morphine" for x in chart(c, pc, include_remote=True)["sections"]["allergy"])

    def peer_qido(purpose: str) -> httpx.Response:
        return httpx.get(f"{a.base}/api/dicom-web/studies", params={"StudyInstanceUID": uid}, timeout=30,
                         headers={"Authorization": f"Bearer {mint(C_OID, A_OID, purpose)}"})

    def peer_wado(purpose: str) -> httpx.Response:
        return httpx.get(f"{a.base}/api/dicom-web/studies/{uid}/metadata", timeout=30,
                         headers={"Authorization": f"Bearer {mint(C_OID, A_OID, purpose)}"})
    assert len(peer_qido("TREAT").json()) == 1 and peer_wado("TREAT").status_code == 200

    # A wildcard refusal at A: no hospital sees her record or her images.
    consent = add(a, pa, "consents", {"category": "deny-sharing", "grantee": "*", "status": "active"})
    view = chart(c, pc, include_remote=True)
    assert not any(x["display"] == "Morphine" for x in view["sections"]["allergy"]) and view["errors"]
    assert peer_qido("TREAT").json() == [] and peer_wado("TREAT").status_code == 403
    pb = register(b, "Private", "Parisa", nat=nat, mrn="B-77", sex="female")
    assert not any(x["display"] == "Morphine" for x in chart(b, pb, include_remote=True)["sections"]["allergy"])
    # Emergency treatment opens both, audited at A (override) and at C (declaration).
    em = chart(c, pc, include_remote=True, purpose="ETREAT")
    assert any(x["display"] == "Morphine" for x in em["sections"]["allergy"])
    assert len(peer_qido("ETREAT").json()) == 1 and peer_wado("ETREAT").status_code == 200
    audit_a = ok(a.get("/api/audit", params={"limit": 500}))["entries"]
    assert any((x["detail"] or {}).get("why") == "emergency override" and x["action"].startswith("fhir.")
               for x in audit_a)
    assert any(x["action"] == "pacs.consent.override" for x in audit_a)
    assert any(x["action"] in ("pacs.consent.deny", "pacs.access") and x["outcome"] == "deny" for x in audit_a)
    audit_c = ok(c.get("/api/audit", params={"limit": 300}))["entries"]
    assert any(x["action"] == "clinical.emergency.declared" and (x["detail"] or {}).get("purpose") == "ETREAT"
               for x in audit_c)
    patch(a, "consent", consent["id"], {"status": "inactive"})
    assert len(peer_qido("TREAT").json()) == 1

    # Opt-in hospital E shares nothing until the patient permits a named hospital.
    nat_e = "6666666666"
    pe = register(e, "Optin", "Omid", nat=nat_e, mrn="E-1")
    add(e, pe, "medications", {"display": "Insulin glargine", "dose": "20 units", "frequency": "at night",
                               "frequency_hours": 24, "status": "active"})
    pc2 = register(c, "Optin", "Omid", nat=nat_e, mrn="C-78")
    pb2 = register(b, "Optin", "Omid", nat=nat_e, mrn="B-78")
    blocked = chart(c, pc2, include_remote=True)
    assert not any(m["display"] == "Insulin glargine" for m in blocked["sections"].get("medication", []))
    assert any("opt-in" in str(err) or "consent" in str(err) for err in blocked["errors"])
    add(e, pe, "consents", {"category": "permit-sharing", "grantee": C_OID, "status": "active"})
    allowed = chart(c, pc2, include_remote=True)
    insulin = next(m for m in allowed["sections"]["medication"] if m["display"] == "Insulin glargine")
    assert insulin["dose"] == "20 units" and insulin["source"]["facility_oid"] == E_OID
    assert not any(m["display"] == "Insulin glargine"
                   for m in chart(b, pb2, include_remote=True)["sections"].get("medication", []))


# --------------------------------------------------------------------------- 8. merge
def test_mpi_merge_after_transfer_moves_record_and_images(net):
    b = net["B"]
    pb = ok(b.get("/api/mpi/search", role="doctor", params={"identifier": NAT_P1}))["results"][0]["person_id"]
    before = chart(b, pb)
    n_rows = {k: len(v) for k, v in before["sections"].items()}
    uids = {s["StudyInstanceUID"] for s in before["imaging"]}
    doc = next(d for d in before["sections"]["document"] if d.get("title") == "Discharge after PCI")
    transfers_before = [t["id"] for t in before["transfers"]]
    # A duplicate registration at B (no shared identifiers) with its own study and order.
    dup = register(b, "Ahmadi", "Dariush", mrn="B-DUP-1")
    add(b, dup, "allergies", {"display": "Aspirin", "reaction": "Bronchospasm"})
    duid, dds = make_study(n_series=1, per_series=1, patient_name="Ahmadi^Dariush", patient_id="B-DUP-1",
                           issuer=b.oid, study_desc="CHEST XR", modality="CR")
    stow(b, dds)
    out = ok(b.post("/api/mpi/merge", role="doctor", json={"survivor_id": dup, "merged_id": pb,
                                                           "reason": "same person"}))
    assert out["survivor"] == dup
    after = chart(b, dup)
    for k, n in n_rows.items():
        assert len(after["sections"].get(k, [])) >= n, k
    assert any(a_["display"] == "Aspirin" for a_ in after["sections"]["allergy"])
    assert uids | {duid} <= {s["StudyInstanceUID"] for s in after["imaging"]}
    assert ok(b.get(f"/api/clinical/documents/{doc['id']}", role="doctor"))["content"].startswith("Discharged on DAPT")
    assert set(transfers_before) <= {t["id"] for t in after["transfers"]}
    met = next(m for m in after["sections"]["medication"] if m["display"] == "Metformin")
    assert len(history(b, "medication", met["id"])) >= 3
    # The old id still resolves to the survivor.
    assert ok(b.get(f"/api/clinical/patients/{pb}/chart", role="doctor"))["person"]["id"] == dup


# --------------------------------------------------------------------------- 9. agent
def test_agent_tools_read_and_act_across_hospitals(net, client, users, monkeypatch):
    """In-process hospital (2.25.1001) + real hospital A: the agent's tools."""
    import asyncio
    from tools.base import registry as tools
    from tests.functional.test_clinical_agent import _ctx
    a = net["A"]
    monkeypatch.setenv("PEER_SECRET", NET_SECRET)
    ok(client.post("/api/facilities", headers=users["admin"], json=a.facility_record(secret_env="PEER_SECRET")))
    ok(a.post("/api/facilities", json={"oid": "2.25.1001", "name": "Test General Hospital", "kind": "hospital",
                                       "base_url": "http://127.0.0.1:9", "secret_env": "PEER_SECRET",
                                       "trust_level": "peer"}))
    pid = ok(client.post("/api/clinical/patients", headers=users["doctor"], json={
        "demographics": {"family": "Visitor", "given": "Vahid", "birth_date": "1966-02-03", "sex": "male"},
        "national_id": NAT_P3, "mrn": "T-1"}))["person_id"]
    ctx = _ctx("doctor_ft")
    res = asyncio.run(tools.get("clinical_patient_summary").run(ctx, person_id=pid, include_remote=True))
    assert not res.error, res.error
    assert "Sulfonamides" in res.content and "Isfahan University Hospital (A)" in res.content
    tl = asyncio.run(tools.get("clinical_timeline").run(ctx, person_id=pid, include_remote=True))
    assert not tl.error and "CT KIDNEYS" in tl.content
    found = asyncio.run(tools.get("clinical_patient_search").run(ctx, query=NAT_P3, scope="network"))
    assert "[at Isfahan University Hospital (A)]" in found.content
    req = asyncio.run(tools.get("request_patient_transfer").run(ctx, person_id=pid, to_facility=A_OID,
                                                                reason="Return to home hospital"))
    assert not req.error, req.error
    t = req.data["transfer"]
    incoming = ok(a.get("/api/transfers", role="doctor", params={"direction": "incoming"}))["transfers"]
    mine = next(x for x in incoming if x["remote_id"] == t["id"])
    assert mine["reason"] == "Return to home hospital" and mine["from_facility"] == "2.25.1001"
