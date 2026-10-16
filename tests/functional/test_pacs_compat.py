"""Functional: device-compatibility behaviour found in vendor conformance
statements (Siemens, Canon, Philips, Hologic, Carestream) and IHE SWF.b.

Each test reproduces one documented device behaviour over a real DICOM
association against the running AranMed SCP.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from pydicom.dataset import Dataset
from pydicom.uid import generate_uid
from pynetdicom import AE, evt
from pynetdicom.sop_class import (ModalityWorklistInformationFind, StorageCommitmentPushModel,
                                  StudyRootQueryRetrieveInformationModelFind, Verification)

from tests.functional.dicom_factory import make_instance
from tests.functional.test_pacs_devices import _make_pki
from tests.functional import test_pacs_dimse as _dimse
from tests.functional.test_pacs_dimse import _Archive, _assoc, _store_all

pacs = _dimse.pacs  # the running-SCP fixture, shared with the DIMSE tests

US_MF_RETIRED = "1.2.840.10008.5.1.4.1.1.3"
US_RETIRED = "1.2.840.10008.5.1.4.1.1.6"


def _wait(cond, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _commit_request(dsets, txn=None) -> Dataset:
    info = Dataset()
    info.TransactionUID = txn or generate_uid()
    refs = []
    for d in dsets:
        r = Dataset()
        r.ReferencedSOPClassUID = d.SOPClassUID
        r.ReferencedSOPInstanceUID = d.SOPInstanceUID
        refs.append(r)
    info.ReferencedSOPSequence = refs
    return info


def _study(n=2, **kw):
    study, series = generate_uid(), generate_uid()
    return [make_instance(study_uid=study, series_uid=series, instance_number=i + 1, **kw)
            for i in range(n)]


# ---------------------------------------------------------------- storage classes
def test_retired_ultrasound_classes_are_accepted(pacs):
    """Philips EPIQ/Affiniti and older GE carts still send the retired US
    and US Multi-frame SOP classes."""
    from pacs import index
    dsets = []
    for uid in (US_MF_RETIRED, US_RETIRED):
        d = _study(1)[0]
        d.SOPClassUID = uid
        d.file_meta.MediaStorageSOPClassUID = uid
        dsets.append(d)
    for d in dsets:
        assert _store_all(pacs["port"], [d]) == [0x0000]
    stored = {i["sop_uid"] for i in index.instance_paths(sop_uids=[d.SOPInstanceUID for d in dsets])}
    assert stored == {d.SOPInstanceUID for d in dsets}


def test_extra_standard_storage_list():
    from pacs.dimse import _extra_standard_storage
    extra = _extra_standard_storage()
    assert US_MF_RETIRED in extra and US_RETIRED in extra
    assert "1.2.840.10008.1.3.10" not in extra           # DICOMDIR is not a storage class
    assert not any(u.startswith("1.2.840.10008.5.1.1.") for u in extra)  # print


# ---------------------------------------------------------------- AE titles
def test_ae_titles_trimmed_case_option_and_rejections_logged(pacs, monkeypatch):
    from clinicaldb import messages
    lower = _assoc(pacs["port"], [Verification], calling="modality")
    assert not lower.is_established and lower.is_rejected       # case-sensitive by default
    log = messages.query(protocol="dicom", limit=20)
    assert any(m["message_type"] == "A-ASSOCIATE-RJ" and m["status"] == "rejected" for m in log)

    monkeypatch.setenv("PACS_AE_CASE_SENSITIVE", "false")
    ae = AE(ae_title="modality ")
    ae.add_requested_context(Verification)
    assoc = ae.associate("127.0.0.1", pacs["port"], ae_title="testpacs")
    assert assoc.is_established
    assert assoc.send_c_echo().Status == 0x0000
    assoc.release()


# ---------------------------------------------------------------- PDU / large objects
def test_unlimited_pdu_advertised_and_node_limit_honoured(pacs, monkeypatch):
    from pacs import nodes, scu
    assoc = _assoc(pacs["port"], [Verification])
    assert assoc.is_established
    assert assoc.acceptor.maximum_length == 0      # PACS_MAX_PDU default: unlimited
    assoc.release()

    seen = {}
    real = AE.associate

    def spy(self, *a, **kw):
        seen["max_pdu"] = kw.get("max_pdu")
        return real(self, *a, **kw)
    monkeypatch.setattr(AE, "associate", spy)
    canon = nodes.save({"name": "Canon CT", "kind": "dimse", "ae_title": "TESTPACS",
                        "host": "127.0.0.1", "port": pacs["port"], "max_pdu": 16384})
    assert scu.echo(canon) is True
    assert seen["max_pdu"] == 16384


def test_large_multiframe_is_streamed_to_disk(pacs, monkeypatch):
    """Enhanced CT / tomosynthesis objects run to hundreds of MB; C-STORE
    must write them to disk instead of materialising them in memory."""
    from pacs import index
    from pacs.storage import get_storage, sha256_file
    seen = []
    real = index.ingest

    def spy(data, **kw):
        seen.append(type(data))
        return real(data, **kw)
    monkeypatch.setattr(index, "ingest", spy)
    d = make_instance(study_uid=generate_uid(), series_uid=generate_uid(), modality="CT",
                      rows=512, cols=512, frames=100)      # ~52 MB of pixel data
    assert _store_all(pacs["port"], [d]) == [0x0000]
    assert seen and issubclass(seen[0], Path)
    inst = index.instance_paths(sop_uids=[d.SOPInstanceUID])[0]
    path = get_storage().root / inst["path"]
    assert path.stat().st_size > 50_000_000
    assert sha256_file(path) == inst["sha256"]


# ---------------------------------------------------------------- MPPS
def test_mpps_rejects_other_sop_classes():
    from pacs import dimse
    ev = SimpleNamespace(request=SimpleNamespace(RequestedSOPClassUID=Verification))
    assert dimse.handle_n_set(ev) == (dimse.NO_SUCH_SOP_CLASS, None)
    ev = SimpleNamespace(request=SimpleNamespace(AffectedSOPClassUID=Verification))
    assert dimse.handle_n_create(ev) == (dimse.NO_SUCH_SOP_CLASS, None)


# ---------------------------------------------------------------- storage commitment
def _send_commit(port, info, calling="MODALITY", keep_open=0.0, handlers=()):
    ae = AE(ae_title=calling)
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = 10
    ae.add_requested_context(StorageCommitmentPushModel)
    assoc = ae.associate("127.0.0.1", port, ae_title="TESTPACS", evt_handlers=list(handlers))
    assert assoc.is_established
    status, _ = assoc.send_n_action(info, 1, StorageCommitmentPushModel, "1.2.840.10008.1.20.1.1")
    if keep_open:
        time.sleep(keep_open)
    assoc.release()
    return status.Status


def test_storage_commitment_durable_retry(pacs):
    """Canon/Philips: report on a new association. The modality's SCP is
    down at first; the report is kept and delivered once it is back, even
    across a restart."""
    import db
    from pacs import stgcmt
    dsets = _study(2)
    assert _store_all(pacs["port"], dsets) == [0, 0]
    info = _commit_request(dsets)
    assert _send_commit(pacs["port"], info) == 0x0000      # modality SCP not running yet
    txn = str(info.TransactionUID)
    assert _wait(lambda: (stgcmt.get(txn) or {}).get("attempts", 0) >= 1)
    row = stgcmt.get(txn)
    assert row["status"] == "pending" and row["last_error"]
    # A crash while sending leaves 'sending'; restart re-queues it.
    with db.connect() as c:
        c.execute("UPDATE pacs_stgcmt SET status='sending', next_attempt_at=0 WHERE transaction_uid=?",
                  (txn,))
    stgcmt.stop_worker()
    stgcmt.start_worker(interval=3600)
    assert stgcmt.get(txn)["status"] == "pending"
    modality = _Archive(pacs["modality_scp_port"], ae_title="MODALITY")
    try:
        assert stgcmt.deliver_due() == 1
        assert modality.events and modality.events[0].TransactionUID == txn
        row = stgcmt.get(txn)
        assert row["status"] == "reported" and row["committed"] == 2 and row["failed"] == 0
    finally:
        modality.stop()


def test_storage_commitment_same_association(pacs):
    """Devices that cannot accept incoming associations wait for the report
    on the association that carried the N-ACTION."""
    from pacs import nodes, stgcmt
    node = nodes.by_ae("MODALITY")
    nodes.save({**node, "stgcmt_mode": "same"}, node["id"])
    dsets = _study(1)
    assert _store_all(pacs["port"], dsets) == [0]
    got = []

    def on_report(event):
        got.append(event.event_information)
        return 0x0000, None
    info = _commit_request(dsets)
    assert _send_commit(pacs["port"], info, keep_open=1.5,
                        handlers=[(evt.EVT_N_EVENT_REPORT, on_report)]) == 0x0000
    assert got and got[0].TransactionUID == info.TransactionUID
    assert _wait(lambda: stgcmt.get(str(info.TransactionUID))["status"] == "reported")
    # Released at once: a same-only device cannot be reached afterwards.
    info2 = _commit_request(dsets)
    assert _send_commit(pacs["port"], info2) == 0x0000
    assert _wait(lambda: stgcmt.get(str(info2.TransactionUID))["status"] == "failed")


def test_storage_commitment_auto_falls_back_to_new_association(pacs):
    """Siemens: waits a few seconds on the same association, then expects
    the report on a new one."""
    from pacs import nodes, stgcmt
    node = nodes.by_ae("MODALITY")
    nodes.save({**node, "stgcmt_mode": "auto"}, node["id"])
    dsets = _study(1)
    assert _store_all(pacs["port"], dsets) == [0]
    modality = _Archive(pacs["modality_scp_port"], ae_title="MODALITY")
    try:
        info = _commit_request(dsets)
        assert _send_commit(pacs["port"], info) == 0x0000      # releases immediately
        assert _wait(lambda: bool(modality.events))
        assert modality.events[0].TransactionUID == info.TransactionUID
        assert _wait(lambda: stgcmt.get(str(info.TransactionUID))["status"] == "reported")
    finally:
        modality.stop()


def test_storage_commitment_api(client, users, pacs):
    from pacs import stgcmt
    stgcmt.record(generate_uid(), "MODALITY", [])
    r = client.get("/api/pacs/stgcmt", headers=users["admin"])
    assert r.status_code == 200
    body = r.json()
    assert body["items"] and "pending" in body["counts"]


# ---------------------------------------------------------------- character sets
def test_latin1_device_gets_romanised_names_in_find_and_worklist(pacs):
    """Canon Aquilion: ISO_IR 100 only. Persian names must arrive legible."""
    from pacs import nodes, worklist
    node = nodes.by_ae("MODALITY")
    nodes.save({**node, "charset": "ISO_IR 100", "name_romanize": True}, node["id"])
    dsets = _study(1, patient_name="محمد^رضایی", patient_id="FA-1", study_desc="سی تی ریه")
    assert _store_all(pacs["port"], dsets) == [0]

    q = Dataset()
    q.QueryRetrieveLevel = "STUDY"
    q.PatientID = "FA-1"
    q.PatientName = ""
    q.StudyDescription = ""
    assoc = _assoc(pacs["port"], [StudyRootQueryRetrieveInformationModelFind])
    hits = [ds for st, ds in assoc.send_c_find(q, StudyRootQueryRetrieveInformationModelFind)
            if st and st.Status in (0xFF00, 0xFF01)]
    assoc.release()
    assert len(hits) == 1
    assert hits[0].SpecificCharacterSet == "ISO_IR 100"
    assert str(hits[0].PatientName) == "Mohammad^Rezaei"
    str(hits[0].StudyDescription).encode("latin-1")            # legible, no mojibake

    worklist.create({"modality": "CT", "patient_id": "FA-2", "patient_name": "فاطمه^حسینی",
                     "station_ae": "MODALITY", "scheduled_start": "2026-10-16T09:00:00"})
    mq = Dataset()
    mq.PatientName = ""
    mq.PatientID = "FA-2"
    sps = Dataset()
    sps.ScheduledStationAETitle = "MODALITY"
    sps.ScheduledProcedureStepStartDate = "20261016"
    sps.Modality = "CT"
    mq.ScheduledProcedureStepSequence = [sps]
    assoc = _assoc(pacs["port"], [ModalityWorklistInformationFind])
    items = [ds for st, ds in assoc.send_c_find(mq, ModalityWorklistInformationFind)
             if st and st.Status in (0xFF00, 0xFF01)]
    assoc.release()
    assert len(items) == 1
    assert items[0].SpecificCharacterSet == "ISO_IR 100"
    assert str(items[0].PatientName) == "Fatemeh^Hosseini"


def test_utf8_device_keeps_original_as_ideographic_group(pacs):
    from pacs import nodes
    node = nodes.by_ae("MODALITY")
    nodes.save({**node, "charset": "ISO_IR 192", "name_romanize": True}, node["id"])
    dsets = _study(1, patient_name="علی^کریمی", patient_id="FA-3")
    assert _store_all(pacs["port"], dsets) == [0]
    q = Dataset()
    q.QueryRetrieveLevel = "STUDY"
    q.PatientID = "FA-3"
    q.PatientName = ""
    assoc = _assoc(pacs["port"], [StudyRootQueryRetrieveInformationModelFind])
    hits = [ds for st, ds in assoc.send_c_find(q, StudyRootQueryRetrieveInformationModelFind)
            if st and st.Status in (0xFF00, 0xFF01)]
    assoc.release()
    pn = hits[0].PatientName
    assert pn.components[0] == "Ali^Karimi" and pn.components[1] == "علی^کریمی"


def test_romanize_unit():
    from pacs.romanize import fit_text, romanize
    assert romanize("محمد^رضایی") == "Mohammad^Rezaei"
    assert romanize("Smith^John") == "Smith^John"
    assert romanize("۱۲۳") == "123"
    assert fit_text("Ünïcode ✓", "ISO_IR 100") == "Ünïcode ?"


# ---------------------------------------------------------------- TLS on its own port
def test_tls_listener_on_separate_port(pacs, tmp_path, monkeypatch):
    from pacs import dimse, nodes, scu
    _make_pki(tmp_path)
    monkeypatch.setenv("PACS_DIMSE_TLS_CERT", str(tmp_path / "server.crt"))
    monkeypatch.setenv("PACS_DIMSE_TLS_KEY", str(tmp_path / "server.key"))
    monkeypatch.setenv("T_CA", str(tmp_path / "ca.crt"))
    srv = dimse.DimseServer(ae_title="TESTPACS", bind="127.0.0.1", port=0, tls_port=0).start()
    try:
        assert srv.tls_port and srv.tls_port != srv.port
        plain = nodes.save({"name": "plain", "kind": "dimse", "ae_title": "TESTPACS",
                            "host": "127.0.0.1", "port": srv.port})
        tls = nodes.save({"name": "tls", "kind": "dimse", "ae_title": "TESTPACS",
                          "host": "127.0.0.1", "port": srv.tls_port, "tls": True,
                          "tls_ca_env": "T_CA"})
        assert scu.echo(plain) is True                    # plain DICOM on the main port
        assert scu.echo(tls) is True                      # TLS on the TLS port
        assert scu.echo({**tls, "tls": False}) is False   # no plaintext on the TLS port
    finally:
        srv.stop()


# ---------------------------------------------------------------- HL7 charset, video
def test_hl7_declared_charset_is_honoured():
    from interop.mllp import decode_hl7
    msg = "MSH|^~\\&|LAB|X|AR|Y|20260101||ADT^A08|1|P|2.5|||||IR|8859/6\rPID|||1||محمد^رضا"
    assert decode_hl7(msg.encode("iso8859-6")).endswith("محمد^رضا")
    legacy = "MSH|^~\\&|LAB\rPID|||1||علي"
    assert decode_hl7(legacy.encode("cp1256")).endswith("علي")
    assert decode_hl7(legacy.encode("utf-8")).endswith("علي")


def test_video_mime_is_honest():
    from pacs import render
    from tests.functional import device_factory as F
    ds = F.make_device("VIDEO")
    data, mime = render.video_stream(ds)
    if mime == "video/mp4":
        assert data[4:8] == b"ftyp"                       # a real MP4 container
    else:
        assert mime in ("video/h264", "video/h265", "video/mpeg")
