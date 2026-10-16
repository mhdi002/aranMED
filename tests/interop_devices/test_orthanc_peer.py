"""AranMed and a real Orthanc server (an independent open-source PACS)
exchanging studies over DICOM (C-ECHO / C-STORE / C-FIND / C-MOVE both
ways) and over Orthanc's REST API."""
from __future__ import annotations

import json
import shutil
import subprocess
import time

import httpx
import pytest
from pydicom.uid import generate_uid

from tests.functional import device_factory as F
from tests.interop_devices.conftest import SCP_AE, free_port

pytestmark = pytest.mark.interop


@pytest.fixture
def orthanc(scp):
    exe = shutil.which("Orthanc")
    if not exe:
        pytest.skip("Orthanc not installed")
    from pacs import nodes
    dicom_port, http_port = free_port(), free_port()
    store = scp["tmp"] / "orthanc"
    store.mkdir()
    cfg = {
        "Name": "ORTHANC-TEST", "StorageDirectory": str(store), "IndexDirectory": str(store),
        "DicomAet": "ORTHANC", "DicomPort": dicom_port, "HttpPort": http_port,
        "RemoteAccessAllowed": False, "AuthenticationEnabled": False, "Plugins": [],
        "DicomCheckCalledAet": True, "DicomAlwaysAllowEcho": True, "DicomAlwaysAllowStore": True,
        "DicomAlwaysAllowFind": True, "DicomAlwaysAllowMove": True, "DicomAlwaysAllowGet": True,
        "DicomModalities": {"aranmed": [SCP_AE, "127.0.0.1", scp["port"]]},
        "StableAge": 1, "ConcurrentJobs": 2, "SaveJobs": False,
    }
    path = scp["tmp"] / "orthanc.json"
    path.write_text(json.dumps(cfg))
    proc = subprocess.Popen([exe, str(path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{http_port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/system", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("Orthanc did not start: " + (proc.stdout.read().decode(errors="replace")[-2000:]))
    dimse_node = nodes.save({"name": "Orthanc (DIMSE)", "kind": "dimse", "ae_title": "ORTHANC",
                             "host": "127.0.0.1", "port": dicom_port, "allow_store": True,
                             "allow_query": True, "allow_retrieve": True,
                             "is_move_destination": True})
    rest_node = nodes.save({"name": "Orthanc (REST)", "kind": "orthanc", "base_url": base})
    yield {"base": base, "dimse": dimse_node, "rest": rest_node}
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _study(n=3, pid="ORT-1", syntax="jpeg_lossless"):
    study = generate_uid()
    return study, [F.encode(F.make_device("CT", study_uid=study, patient_id=pid), syntax)
                   for _ in range(n)]


@pytest.mark.device(vendor="Orthanc 1.12 (peer PACS)", service="C-ECHO and C-STORE AranMed → Orthanc")
def test_aranmed_sends_to_orthanc(orthanc):
    from pacs import scu
    assert scu.echo(orthanc["dimse"]) is True
    study, dsets = _study()
    assert scu.store(orthanc["dimse"], [F.to_bytes(d) for d in dsets]) == {"sent": 3, "failed": 0}
    found = httpx.post(f"{orthanc['base']}/tools/find", json={
        "Level": "Instance", "Query": {"StudyInstanceUID": study}}).json()
    assert len(found) == 3


@pytest.mark.device(vendor="Orthanc 1.12 (peer PACS)", service="C-FIND + C-MOVE Orthanc → AranMed (we retrieve)")
def test_aranmed_queries_and_moves_from_orthanc(orthanc):
    from pacs import index, scu
    study, dsets = _study(pid="ORT-2")
    for d in dsets:
        r = httpx.post(f"{orthanc['base']}/instances", content=F.to_bytes(d),
                       headers={"Content-Type": "application/dicom"})
        assert r.status_code == 200
    hits = scu.find(orthanc["dimse"], "STUDY", {"PatientID": "ORT-2"}, ["StudyInstanceUID"])
    assert [str(h.get("StudyInstanceUID")) for h in hits] == [study]
    res = scu.move(orthanc["dimse"], study_uid=study)
    assert res["completed"] == 3 and res["failed"] == 0
    assert len(index.instance_paths(study_uid=study)) == 3


@pytest.mark.device(vendor="Orthanc 1.12 (peer PACS)", service="Orthanc pushes (C-STORE) to AranMed; Orthanc C-MOVEs from AranMed")
def test_orthanc_pushes_and_retrieves(orthanc, scp):
    from pacs import index
    study, dsets = _study(pid="ORT-3")
    ids = []
    for d in dsets:
        ids.append(httpx.post(f"{orthanc['base']}/instances", content=F.to_bytes(d)).json()["ID"])
    r = httpx.post(f"{orthanc['base']}/modalities/aranmed/store", json={"Resources": ids,
                                                                         "Synchronous": True},
                   timeout=60)
    assert r.status_code == 200, r.text
    assert len(index.instance_paths(study_uid=study)) == 3

    # A study only AranMed holds, pulled by Orthanc with C-MOVE (Orthanc as
    # Q/R SCU, AranMed sending to the registered ORTHANC destination).
    s2, d2 = _study(pid="ORT-4")
    for d in d2:
        index.ingest(F.to_bytes(d), source="test")
    r = httpx.post(f"{orthanc['base']}/modalities/aranmed/move", json={
        "Level": "Study", "Resources": [{"StudyInstanceUID": s2}], "TargetAet": "ORTHANC",
        "Synchronous": True}, timeout=60)
    assert r.status_code == 200, r.text
    found = httpx.post(f"{orthanc['base']}/tools/find", json={
        "Level": "Instance", "Query": {"StudyInstanceUID": s2}}).json()
    assert len(found) == 3


@pytest.mark.device(vendor="Orthanc 1.12 (peer PACS)", service="Orthanc REST adapter: search, retrieve, send")
def test_orthanc_rest_adapter(orthanc):
    from pacs import index
    from pacs.adapters import for_node
    study, dsets = _study(pid="ORT-5")
    for d in dsets:
        httpx.post(f"{orthanc['base']}/instances", content=F.to_bytes(d))
    ad = for_node(orthanc["rest"])
    assert ad.echo() is True
    assert [s["StudyInstanceUID"] for s in ad.search_studies({"PatientID": "ORT-5"})] == [study]
    assert ad.retrieve_study(study)["stored"] == 3
    assert len(index.instance_paths(study_uid=study)) == 3
    s2, d2 = _study(pid="ORT-6")
    for d in d2:
        index.ingest(F.to_bytes(d), source="test")
    assert ad.send_study(s2)["sent"] == 3
