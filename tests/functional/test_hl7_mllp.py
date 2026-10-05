"""Functional: HL7 v2 over a real MLLP socket — the hospital's ADT/order/result
feeds driving the MPI, the EHR and the PACS worklist."""
from __future__ import annotations

import time

import pytest

from interop import mllp

SEND = "MSH|^~\\&|HIS|CITYHOSP^2.25.1001^ISO|ARANMED|TGH|{ts}||{type}|{cid}|P|2.5"


def msg(type_: str, cid: str, *segments: str) -> str:
    ts = time.strftime("%Y%m%d%H%M%S")
    return "\r".join([SEND.format(ts=ts, type=type_, cid=cid), *segments])


@pytest.fixture
def mllp_server(client):
    srv = mllp.MllpServer(bind="127.0.0.1", port=0).start()
    yield srv
    srv.stop()


def send(srv, text: str) -> str:
    return mllp.send("127.0.0.1", srv.port, text)


def test_adt_orders_results_documents_queries(mllp_server, client, users):
    from clinicaldb import mpi, messages
    from ehr import store
    doc = users["doctor"]

    # A04 register outpatient + visit, with allergy.
    ack = send(mllp_server, msg(
        "ADT^A04^ADT_A01", "C1", "EVN|A04|20261001080000",
        "PID|1||H-9001^^^&2.25.1001&ISO^MR~0011223344^^^&2.16.364&ISO^NI||Tavakoli^Maryam^S||19880214|F|||Azadi St 1^^Tehran||+982155500",
        "PV1|1|O|CLINIC^R12^B1^CARDIO||||D001^Rezaei^Hamid||||||||||||V-777|||||||||||||||||||||||||20261001080000",
        "AL1|1|DA|PCN^Penicillin|SV|Rash"))
    assert "MSA|AA|C1" in ack
    pid = mpi.find_by_identifier("urn:oid:2.25.1001", "H-9001")
    p = mpi.get(pid)
    assert p["demographics"]["family"] == "Tavakoli" and p["demographics"]["birth_date"] == "1988-02-14"
    assert {"urn:oid:2.25.1001", "urn:aranmed:national-id"} <= {i["system"] for i in p["identifiers"]}
    encs = store.list_for("encounter", pid)
    assert encs[0]["status"] == "arrived" and encs[0]["department"] == "CARDIO"
    assert encs[0]["attending"] == "Rezaei Hamid"
    assert store.list_for("allergy", pid)[0]["display"] == "Penicillin"

    # A08 update demographics; A03 discharge closes the same visit.
    send(mllp_server, msg("ADT^A08", "C2", "PID|1||H-9001^^^&2.25.1001&ISO^MR||Tavakoli-Nik^Maryam||19880214|F",
                          "PV1|1|O|CLINIC||||||||||||||||V-777"))
    assert mpi.get(pid)["demographics"]["family"] == "Tavakoli-Nik"
    send(mllp_server, msg("ADT^A03", "C3", "EVN|A03|20261001120000",
                          "PID|1||H-9001^^^&2.25.1001&ISO^MR||Tavakoli-Nik^Maryam||19880214|F",
                          "PV1|1|O|CLINIC||||||||||||||||V-777|||||||||||||||||01||||||||20261001080000|20261001120000"))
    encs = store.list_for("encounter", pid)
    assert len(encs) == 1 and encs[0]["status"] == "finished" and encs[0]["end_at"].startswith("2026-10-01T12")

    # A40 merge: a duplicate registered under another MRN is merged into the survivor.
    send(mllp_server, msg("ADT^A28", "C4", "PID|1||H-9002^^^&2.25.1001&ISO^MR||Tavakoly^Mariam||19880214|F"))
    dup = mpi.find_by_identifier("urn:oid:2.25.1001", "H-9002")
    assert dup and dup != pid
    ack = send(mllp_server, msg("ADT^A40", "C5", "EVN|A40",
                                "PID|1||H-9001^^^&2.25.1001&ISO^MR||Tavakoli-Nik^Maryam",
                                "MRG|H-9002^^^&2.25.1001&ISO^MR"))
    assert "MSA|AA|C5" in ack
    assert mpi.find_by_identifier("urn:oid:2.25.1001", "H-9002") == pid

    # ORM imaging order -> ServiceRequest -> modality worklist.
    ack = send(mllp_server, msg(
        "ORM^O01", "C6", "PID|1||H-9001^^^&2.25.1001&ISO^MR||Tavakoli-Nik^Maryam||19880214|F",
        "ORC|NW|PL-55|FL-55||SC||^^^20261002090000^^S",
        "OBR|1|PL-55|FL-55|CTH^CT Head without contrast|||20261002090000|||||||||D001^Rezaei^Hamid||||||||CT|||||||^Headache"))
    assert "MSA|AA|C6" in ack
    sr = store.list_for("service_request", pid)[0]
    assert sr["category"] == "imaging" and sr["priority"] == "stat" and sr["accession"] == "FL-55"
    wl = client.get("/api/pacs/worklist", headers=doc).json()["items"]
    item = next(w for w in wl if w["order_id"] == sr["id"])
    assert item["modality"] == "CT" and item["person_id"] == pid
    # ORC CA cancels the order and its worklist entry.
    send(mllp_server, msg("ORM^O01", "C7", "PID|1||H-9001^^^&2.25.1001&ISO^MR||X^Y",
                          "ORC|CA|PL-55|FL-55", "OBR|1|PL-55|FL-55|CTH^CT Head"))
    assert store.get("service_request", sr["id"])["status"] == "revoked"
    cancelled = next(w for w in client.get("/api/pacs/worklist", headers=doc).json()["items"]
                     if w["order_id"] == sr["id"])
    assert cancelled["status"] == "cancelled"

    # ORU lab results with ranges and flags.
    ack = send(mllp_server, msg(
        "ORU^R01", "C8", "PID|1||H-9001^^^&2.25.1001&ISO^MR||Tavakoli-Nik^Maryam",
        "OBR|1||LAB-1|24323-8^Comprehensive metabolic panel^LN|||20261002070000|||||||||||||||||CH|F",
        "OBX|1|NM|2345-7^Glucose^LN||182|mg/dL|70-99|H|||F|||20261002070000",
        "OBX|2|NM|2160-0^Creatinine^LN||0.9|mg/dL|0.6-1.2|N|||F",
        "OBX|3|ST|5778-6^Color of urine^LN||Yellow||||||F"))
    assert "MSA|AA|C8" in ack
    labs = {o["display"]: o for o in store.list_for("observation", pid)}
    assert labs["Glucose"]["value_num"] == 182 and labs["Glucose"]["interpretation"] == "H"
    assert labs["Glucose"]["ref_high"] == 99 and labs["Glucose"]["code_system"] == "http://loinc.org"
    assert labs["Color of urine"]["value_text"] == "Yellow"
    rep = store.list_for("diagnostic_report", pid)[0]
    assert rep["category"] == "LAB" and len(rep["result_ids"]) == 3
    # ORU radiology narrative.
    send(mllp_server, msg("ORU^R01", "C9", "PID|1||H-9001^^^&2.25.1001&ISO^MR||T^M",
                          "OBR|1||RAD-9|CTH^CT Head|||20261002100000|||||||||||||||||RAD|F",
                          "OBX|1|TX|&GDT^Report||FINDINGS: No hemorrhage.||||||F",
                          "OBX|2|TX|&GDT^Report||IMPRESSION: Normal CT head.||||||F"))
    rad = next(r for r in store.list_for("diagnostic_report", pid) if r["category"] == "RAD")
    assert rad["conclusion"] == "Normal CT head." and "No hemorrhage" in rad["text"]

    # MDM document, VXU immunization.
    send(mllp_server, msg("MDM^T02", "C10", "EVN|T02", "PID|1||H-9001^^^&2.25.1001&ISO^MR||T^M",
                          "TXA|1|DS|TX|20261002120000||||||||DOC-1||||Discharge summary|AU",
                          "OBX|1|TX|||Discharged in good condition.\\.br\\Follow up in 2 weeks.||||||F"))
    d = store.list_for("document", pid)[0]
    full = store.get("document", d["id"])
    assert full["doc_type"] == "discharge-summary" and "Follow up in 2 weeks." in full["content"]
    send(mllp_server, msg("VXU^V04", "C11", "PID|1||H-9001^^^&2.25.1001&ISO^MR||T^M",
                          "RXA|0|1|20250101||140^Influenza^CVX|0.5|mL||||||||LOT77"))
    imm = store.list_for("immunization", pid)[0]
    assert imm["display"] == "Influenza" and imm["lot"] == "LOT77"

    # QBP PDQ (Q22) and PIX (Q23).
    rsp = send(mllp_server, msg("QBP^Q22^QBP_Q21", "C12",
                                "QPD|IHE PDQ Query|Q1|@PID.5.1^Tavakoli-Nik~@PID.7^19880214", "RCP|I"))
    assert rsp.startswith("MSH") and "RSP^K22" in rsp and "QAK|Q1|OK" in rsp and "Tavakoli-Nik" in rsp
    rsp = send(mllp_server, msg("QBP^Q23^QBP_Q21", "C13",
                                "QPD|IHE PIX Query|Q2|H-9001^^^&2.25.1001&ISO^MR", "RCP|I"))
    assert "0011223344" in rsp and "H-9002" in rsp

    # Errors: garbage -> AR; MDM for an unknown patient -> AE; unknown type -> AR.
    assert "MSA|AR" in send(mllp_server, "NOT HL7")
    assert "MSA|AE|C14" in send(mllp_server, msg("MDM^T02", "C14", "PID|1||NOBODY^^^&2.25.1001&ISO^MR",
                                                  "TXA|1|PN", "OBX|1|TX|||x"))
    assert "MSA|AR|C15" in send(mllp_server, msg("ZZZ^Z01", "C15", "PID|1||H-9001"))

    # Every exchange is in the interop log; HL7 over HTTP works too.
    log = messages.query(protocol="hl7v2", limit=100)
    assert {m["status"] for m in log} >= {"ok", "rejected", "error"}
    r = client.post("/api/interop/hl7", headers=doc,
                    content=msg("ADT^A08", "C16", "PID|1||H-9001^^^&2.25.1001&ISO^MR||Tavakoli-Nik^Maryam||19880214|F"))
    assert "MSA|AA|C16" in r.text
    log = client.get("/api/interop/messages", headers=users["admin"], params={"protocol": "hl7v2"}).json()
    assert len(log["messages"]) >= 16


def test_mllp_multiple_messages_one_connection(mllp_server, client):
    import socket
    s = socket.create_connection(("127.0.0.1", mllp_server.port), timeout=10)
    try:
        payload = b"".join(mllp.frame(msg("ADT^A28", f"M{i}", f"PID|1||MULTI-{i}^^^&2.25.1001&ISO^MR||Multi^P{i}"))
                           for i in range(3))
        s.sendall(payload)
        buf = b""
        while buf.count(b"\x1c\x0d") < 3:
            buf += s.recv(65536)
    finally:
        s.close()
    acks = [a for a in buf.split(b"\x1c\x0d") if a]
    assert len(acks) == 3 and all(b"MSA|AA" in a for a in acks)
