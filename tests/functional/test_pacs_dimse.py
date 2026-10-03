"""Functional: the PACS over real DICOM networking (pynetdicom on TCP).

A simulated modality (independent pynetdicom SCU) and a simulated second
archive (independent pynetdicom Storage SCP) talk to the running AranMed SCP.
Every step goes over a real association; assertions check both the DIMSE
statuses and the resulting index/storage state.
"""
from __future__ import annotations

import threading
import time

import pytest
from pydicom.dataset import Dataset
from pydicom.uid import generate_uid
from pynetdicom import AE, AllStoragePresentationContexts, StoragePresentationContexts, build_role, evt
from pynetdicom.sop_class import (ModalityPerformedProcedureStep,
                                  ModalityWorklistInformationFind,
                                  PatientRootQueryRetrieveInformationModelFind,
                                  StorageCommitmentPushModel,
                                  StudyRootQueryRetrieveInformationModelFind,
                                  StudyRootQueryRetrieveInformationModelGet,
                                  StudyRootQueryRetrieveInformationModelMove, Verification)

from tests.functional.conftest import free_port
from tests.functional.dicom_factory import make_instance, make_study, to_bytes


@pytest.fixture
def pacs(clinical_env):
    """Running AranMed SCP + registered peers; yields a context dict."""
    from pacs import dimse, nodes
    from clinicaldb import facilities
    facilities.seed_local()
    archive_port = free_port()
    modality_scp_port = free_port()
    nodes.save({"name": "CT Scanner 1", "kind": "dimse", "ae_title": "MODALITY",
                "host": "127.0.0.1", "port": modality_scp_port, "allow_store": True,
                "allow_query": True, "allow_retrieve": True, "is_move_destination": True})
    nodes.save({"name": "Second Archive", "kind": "dimse", "ae_title": "ARCHIVE2",
                "host": "127.0.0.1", "port": archive_port, "allow_store": True,
                "allow_query": True, "allow_retrieve": True, "is_move_destination": True})
    nodes.save({"name": "Viewer Only", "kind": "dimse", "ae_title": "READONLY",
                "host": "127.0.0.1", "port": free_port(), "allow_store": False,
                "allow_query": True, "allow_retrieve": False, "is_move_destination": False})
    server = dimse.DimseServer(ae_title="TESTPACS", bind="127.0.0.1", port=0).start()
    yield {"port": server.port, "archive_port": archive_port,
           "modality_scp_port": modality_scp_port}
    server.stop()


def _assoc(port, contexts, calling="MODALITY", **kw):
    ae = AE(ae_title=calling)
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = 10
    for cx in contexts:
        ae.add_requested_context(cx)
    return ae.associate("127.0.0.1", port, ae_title="TESTPACS", **kw)


def _store_all(port, datasets, calling="MODALITY"):
    classes = sorted({str(d.SOPClassUID) for d in datasets})
    assoc = _assoc(port, classes, calling=calling)
    assert assoc.is_established
    statuses = [assoc.send_c_store(d).Status for d in datasets]
    assoc.release()
    return statuses


class _Archive:
    """Independent Storage SCP standing in for another PACS."""

    def __init__(self, port, ae_title="ARCHIVE2", extra_handlers=()):
        self.received: dict[str, bytes] = {}
        self.events: list = []
        ae = AE(ae_title=ae_title)
        for cx in AllStoragePresentationContexts:
            ae.add_supported_context(cx.abstract_syntax)
        ae.add_supported_context(StorageCommitmentPushModel, scu_role=True, scp_role=True)

        def on_store(event):
            ds = event.dataset
            self.received[str(ds.SOPInstanceUID)] = ds.PixelData
            return 0x0000

        def on_report(event):
            self.events.append(event.event_information)
            return 0x0000, None

        self.server = ae.start_server(("127.0.0.1", port), block=False, evt_handlers=[
            (evt.EVT_C_STORE, on_store), (evt.EVT_N_EVENT_REPORT, on_report), *extra_handlers])

    def stop(self):
        self.server.shutdown()


def test_echo_and_association_allowlist(pacs):
    assoc = _assoc(pacs["port"], [Verification])
    assert assoc.is_established
    assert assoc.send_c_echo().Status == 0x0000
    assoc.release()
    # An AE that is not registered is rejected at association time.
    stranger = _assoc(pacs["port"], [Verification], calling="STRANGER")
    assert not stranger.is_established
    assert stranger.is_rejected


def test_store_query_retrieve_full_cycle(pacs):
    from pacs import index
    from clinicaldb import mpi
    study_uid, dsets = make_study(n_series=3, per_series=4, patient_name="Rahimi^Neda",
                                  patient_id="CT-555", accession="ACC-900",
                                  study_date="20260915", sex="F", birth_date="19900302")
    # --- C-STORE: 12 objects, all succeed, all indexed and on disk ----------
    assert _store_all(pacs["port"], dsets) == [0x0000] * 12
    study = index.get_study(study_uid)
    assert study["NumberOfStudyRelatedSeries"] == 3
    assert study["NumberOfStudyRelatedInstances"] == 12
    assert study["ModalitiesInStudy"] == ["CT"]
    assert index.verify_study(study_uid) == {"ok": 12, "corrupt": [], "missing": []}
    person = mpi.get(study["_ext"]["person_id"])
    assert person["demographics"]["family"] == "Rahimi"
    assert {"urn:oid:2.25.1001", "CT-555"} <= {person["identifiers"][0]["system"],
                                              person["identifiers"][0]["value"]}

    # Re-sending is idempotent.
    assert _store_all(pacs["port"], dsets[:2]) == [0x0000, 0x0000]
    assert index.get_study(study_uid)["NumberOfStudyRelatedInstances"] == 12

    # --- C-FIND at every level -------------------------------------------------
    assoc = _assoc(pacs["port"], [StudyRootQueryRetrieveInformationModelFind,
                                  PatientRootQueryRetrieveInformationModelFind])
    assert assoc.is_established

    def find(q, model=StudyRootQueryRetrieveInformationModelFind):
        return [ident for st, ident in assoc.send_c_find(q, model)
                if st and st.Status == 0xFF00]

    q = Dataset()
    q.QueryRetrieveLevel = "STUDY"
    q.PatientName = "rahimi*"
    q.StudyDate = "20260901-20260930"
    q.ModalitiesInStudy = "CT"
    q.StudyInstanceUID = ""
    q.NumberOfStudyRelatedInstances = ""
    hits = find(q)
    assert len(hits) == 1
    assert hits[0].StudyInstanceUID == study_uid
    assert int(hits[0].NumberOfStudyRelatedInstances) == 12
    assert hits[0].RetrieveAETitle == "TESTPACS"

    q.StudyDate = "20250101-20250131"
    assert find(q) == []

    q = Dataset()
    q.QueryRetrieveLevel = "SERIES"
    q.StudyInstanceUID = study_uid
    q.SeriesInstanceUID = ""
    q.SeriesNumber = ""
    series = find(q)
    assert sorted(int(s.SeriesNumber) for s in series) == [1, 2, 3]

    q = Dataset()
    q.QueryRetrieveLevel = "IMAGE"
    q.StudyInstanceUID = study_uid
    q.SeriesInstanceUID = series[0].SeriesInstanceUID
    q.SOPInstanceUID = ""
    assert len(find(q)) == 4

    q = Dataset()
    q.QueryRetrieveLevel = "PATIENT"
    q.PatientID = "CT-555"
    q.NumberOfPatientRelatedStudies = ""
    pats = find(q, PatientRootQueryRetrieveInformationModelFind)
    assert len(pats) == 1 and int(pats[0].NumberOfPatientRelatedStudies) == 1
    assoc.release()

    # --- C-MOVE the study to the second archive ------------------------------
    archive = _Archive(pacs["archive_port"])
    try:
        assoc = _assoc(pacs["port"], [StudyRootQueryRetrieveInformationModelMove])
        q = Dataset()
        q.QueryRetrieveLevel = "STUDY"
        q.StudyInstanceUID = study_uid
        final = [st for st, _ in assoc.send_c_move(q, "ARCHIVE2",
                                                   StudyRootQueryRetrieveInformationModelMove)][-1]
        assert final.Status == 0x0000
        assert int(final.NumberOfCompletedSuboperations) == 12
        # Pixel data arrived byte-identical.
        sent = {str(d.SOPInstanceUID): d.PixelData for d in dsets}
        assert archive.received == sent

        # A destination nobody registered is refused (no exfiltration).
        final = [st for st, _ in assoc.send_c_move(q, "EVIL",
                                                   StudyRootQueryRetrieveInformationModelMove)][-1]
        assert final.Status == 0xA801
        assoc.release()
    finally:
        archive.stop()

    # --- C-GET one series on the same association ------------------------------
    got: list = []

    def on_store(event):
        got.append(str(event.dataset.SOPInstanceUID))
        return 0x0000

    ae = AE(ae_title="MODALITY")
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelGet)
    ae.add_requested_context(str(dsets[0].SOPClassUID))
    role = build_role(str(dsets[0].SOPClassUID), scp_role=True)
    assoc = ae.associate("127.0.0.1", pacs["port"], ae_title="TESTPACS", ext_neg=[role],
                         evt_handlers=[(evt.EVT_C_STORE, on_store)])
    q = Dataset()
    q.QueryRetrieveLevel = "SERIES"
    q.StudyInstanceUID = study_uid
    q.SeriesInstanceUID = series[1].SeriesInstanceUID
    final = [st for st, _ in assoc.send_c_get(q, StudyRootQueryRetrieveInformationModelGet)][-1]
    assoc.release()
    assert final.Status == 0x0000
    assert len(got) == 4


def test_per_node_permissions(pacs):
    study_uid, dsets = make_study(n_series=1, per_series=1)
    # READONLY may connect and query but not store.
    assert _store_all(pacs["port"], dsets, calling="READONLY") == [0x0124]
    from pacs import index
    assert index.get_study(study_uid) is None


def test_scheduled_workflow_mwl_mpps(pacs):
    """Order -> worklist -> modality MWL query -> MPPS -> images -> completed."""
    from pacs import index, worklist
    wl = worklist.create({"patient_name": "Moradi^Reza", "patient_id": "MR-77",
                          "patient_birth_date": "1975-05-05", "patient_sex": "M",
                          "modality": "MR", "station_ae": "MODALITY",
                          "scheduled_start": "20261003T090000",
                          "procedure_code": "MRBRAIN", "procedure_description": "MRI Brain",
                          "priority": "STAT", "accession": "ACC-MWL-1"})
    assert wl["status"] == "scheduled"

    # Modality pulls its worklist.
    assoc = _assoc(pacs["port"], [ModalityWorklistInformationFind,
                                  ModalityPerformedProcedureStep])
    assert assoc.is_established
    q = Dataset()
    q.PatientName = ""
    q.PatientID = ""
    q.AccessionNumber = ""
    q.StudyInstanceUID = ""
    sps = Dataset()
    sps.Modality = "MR"
    sps.ScheduledStationAETitle = "MODALITY"
    sps.ScheduledProcedureStepStartDate = "20261003"
    q.ScheduledProcedureStepSequence = [sps]
    items = [i for st, i in assoc.send_c_find(q, ModalityWorklistInformationFind)
             if st and st.Status == 0xFF00]
    assert len(items) == 1
    item = items[0]
    assert item.AccessionNumber == "ACC-MWL-1"
    assert str(item.PatientName) == "Moradi^Reza"
    assert item.StudyInstanceUID == wl["study_uid"]
    assert item.ScheduledProcedureStepSequence[0].ScheduledProtocolCodeSequence[0].CodeValue == "MRBRAIN"

    # MPPS N-CREATE (IN PROGRESS).
    mpps_uid = generate_uid()
    attrs = Dataset()
    attrs.PerformedProcedureStepStatus = "IN PROGRESS"
    attrs.Modality = "MR"
    attrs.PerformedProcedureStepStartDate = "20261003"
    attrs.PerformedProcedureStepStartTime = "091000"
    ssa = Dataset()
    ssa.AccessionNumber = "ACC-MWL-1"
    ssa.StudyInstanceUID = item.StudyInstanceUID
    attrs.ScheduledStepAttributesSequence = [ssa]
    status, _ = assoc.send_n_create(attrs, ModalityPerformedProcedureStep, mpps_uid)
    assert status.Status == 0x0000
    assert worklist.get("ACC-MWL-1")["status"] == "in_progress"

    # Images under the worklist's study UID / accession.
    series_uid = generate_uid()
    images = [make_instance(study_uid=item.StudyInstanceUID, series_uid=series_uid,
                            modality="MR", instance_number=i + 1, patient_name="Moradi^Reza",
                            patient_id="MR-77", accession="ACC-MWL-1") for i in range(3)]
    assert _store_all(pacs["port"], images) == [0x0000] * 3
    study = index.get_study(item.StudyInstanceUID)
    assert study["_ext"]["person_id"] == wl["person_id"]  # same MPI person as the order
    assert study["_ext"]["priority"] == "STAT"

    # MPPS N-SET COMPLETED.
    mod = Dataset()
    mod.PerformedProcedureStepStatus = "COMPLETED"
    mod.PerformedProcedureStepEndDate = "20261003"
    mod.PerformedProcedureStepEndTime = "092000"
    perf = Dataset()
    perf.SeriesInstanceUID = series_uid
    refs = []
    for img in images:
        r = Dataset()
        r.ReferencedSOPClassUID = img.SOPClassUID
        r.ReferencedSOPInstanceUID = img.SOPInstanceUID
        refs.append(r)
    perf.ReferencedImageSequence = refs
    mod.PerformedSeriesSequence = [perf]
    status, _ = assoc.send_n_set(mod, ModalityPerformedProcedureStep, mpps_uid)
    assert status.Status == 0x0000
    # A second completion is rejected: the step is already final.
    status, _ = assoc.send_n_set(mod, ModalityPerformedProcedureStep, mpps_uid)
    assert status.Status == 0xC310
    assoc.release()
    assert worklist.get("ACC-MWL-1")["status"] == "completed"
    assert worklist.list_mpps()[0]["status"] == "COMPLETED"


def test_storage_commitment_report(pacs):
    """N-ACTION is answered by an N-EVENT-REPORT on a new association."""
    study_uid, dsets = make_study(n_series=1, per_series=2)
    assert _store_all(pacs["port"], dsets) == [0x0000, 0x0000]
    requester_scp = _Archive(pacs["modality_scp_port"], ae_title="MODALITY")
    try:
        ae = AE(ae_title="MODALITY")
        ae.add_requested_context(StorageCommitmentPushModel)
        assoc = ae.associate("127.0.0.1", pacs["port"], ae_title="TESTPACS")
        info = Dataset()
        info.TransactionUID = generate_uid()
        refs = []
        for d in dsets:
            r = Dataset()
            r.ReferencedSOPClassUID = d.SOPClassUID
            r.ReferencedSOPInstanceUID = d.SOPInstanceUID
            refs.append(r)
        missing = Dataset()
        missing.ReferencedSOPClassUID = dsets[0].SOPClassUID
        missing.ReferencedSOPInstanceUID = generate_uid()
        info.ReferencedSOPSequence = refs + [missing]
        status, _ = assoc.send_n_action(info, 1, StorageCommitmentPushModel,
                                        "1.2.840.10008.1.20.1.1")
        assoc.release()
        assert status.Status == 0x0000
        deadline = time.time() + 10
        while not requester_scp.events and time.time() < deadline:
            time.sleep(0.05)
        assert requester_scp.events, "no N-EVENT-REPORT received"
        report = requester_scp.events[0]
        assert report.TransactionUID == info.TransactionUID
        assert {r.ReferencedSOPInstanceUID for r in report.ReferencedSOPSequence} == \
            {d.SOPInstanceUID for d in dsets}
        assert report.FailedSOPSequence[0].ReferencedSOPInstanceUID == \
            missing.ReferencedSOPInstanceUID
    finally:
        requester_scp.stop()


def test_scu_module_against_scp(pacs):
    """Our own SCU (used for outbound work) interoperates with an SCP."""
    from pacs import index, nodes, scu
    me = {"ae_title": "TESTPACS", "host": "127.0.0.1", "port": pacs["port"]}
    # Calls from our SCU carry our AE title, so register ourselves as a peer.
    nodes.save({"name": "Self", "kind": "dimse", "ae_title": "TESTPACS",
                "host": "127.0.0.1", "port": pacs["port"]})
    assert scu.echo(me) is True
    study_uid, dsets = make_study(n_series=1, per_series=3, patient_id="SCU-1")
    assert scu.store(me, [to_bytes(d) for d in dsets]) == {"sent": 3, "failed": 0}
    found = scu.find(me, "STUDY", {"PatientID": "SCU-1"}, ["StudyInstanceUID"])
    assert [f["StudyInstanceUID"] for f in found] == [study_uid]
    assert scu.echo({"ae_title": "NOPE", "host": "127.0.0.1", "port": free_port()}) is False
    assert index.get_study(study_uid)["NumberOfStudyRelatedInstances"] == 3
