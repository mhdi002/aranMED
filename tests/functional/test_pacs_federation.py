"""Functional: imaging across hospitals and external archives.

Two independent AranMed hospitals (separate server processes, databases,
storage, DICOM listeners) plus a fake Orthanc. Scenarios:

1. Hospital A finds a study held only at hospital B (federated QIDO with a
   signed peer token), retrieves it over WADO-RS, and B's audit trail
   records who at A accessed it and why.
2. The same over classic DICOM: C-FIND + C-MOVE between the two PACS.
3. A pushes a study to B (STOW-RS with a peer token).
4. A peer with the wrong shared secret is refused and the search degrades.
5. Orthanc REST adapter: search, retrieve, send.
"""
from __future__ import annotations

import pytest

from tests.functional.conftest import free_port
from tests.functional.dicom_factory import make_study, to_bytes
from tests.functional.hospital import Hospital

SECRET = "shared-secret-A-B-0123456789"


@pytest.fixture(scope="module")
def hospitals(tmp_path_factory):
    work = tmp_path_factory.mktemp("hospitals")
    a = Hospital("Hospital A", "2.25.111", work, ae_title="HOSPA",
                 extra_env={"PEER_SECRET_B": SECRET, "PEER_SECRET_WRONG": "not-the-secret"})
    b = Hospital("Hospital B", "2.25.222", work, ae_title="HOSPB",
                 extra_env={"PEER_SECRET_A": SECRET})
    a.start()
    b.start()
    try:
        # Mutual registration: each knows the other's endpoints + shared secret.
        assert a.post("/api/facilities", json=b.facility_record(secret_env="PEER_SECRET_B")).status_code == 200
        assert b.post("/api/facilities", json=a.facility_record(secret_env="PEER_SECRET_A")).status_code == 200
        yield a, b
    finally:
        a.stop()
        b.stop()


def _stow(h: Hospital, dsets):
    from pacs import multipart
    bnd = multipart.boundary()
    body = multipart.encode(((to_bytes(d), "application/dicom") for d in dsets), bnd)
    r = h.post("/api/dicom-web/studies", content=body,
               headers={"Content-Type": multipart.content_type(bnd, "application/dicom")})
    assert r.status_code == 200, r.text


def test_federated_search_and_wado_retrieve_between_hospitals(hospitals):
    a, b = hospitals
    study_uid, dsets = make_study(n_series=2, per_series=3, patient_name="Ebrahimi^Mina",
                                  patient_id="B-1001", issuer="2.25.222", accession="B-ACC-1")
    _stow(b, dsets)

    # A's capabilities check of B: same schema versions -> compatible.
    caps_a = a.get("/api/interop/capabilities").json()
    caps_b = b.get("/api/interop/capabilities").json()
    assert caps_a["schema"] == caps_b["schema"]

    # Not at A locally...
    local = a.get("/api/pacs/studies", role="doctor", params={"patient_id": "B-1001"}).json()
    assert local["total"] == 0
    # ...but federated search finds it at B.
    fed = a.get("/api/pacs/studies", role="doctor",
                params={"patient_id": "B-1001", "scope": "all"}).json()
    assert fed["errors"] == []
    assert [s["StudyInstanceUID"] for s in fed["studies"]] == [study_uid]
    loc = fed["studies"][0]["locations"]
    assert loc == [{"kind": "dicomweb", "id": "facility:2.25.222", "name": "Hospital B",
                    "facility_oid": "2.25.222"}]

    # Retrieve into A over WADO-RS.
    job = a.post("/api/pacs/retrieve", role="doctor",
                 json={"study_uid": study_uid, "node_id": "facility:2.25.222",
                       "wait": True}).json()
    assert job["status"] == "done", job
    assert job["result"] == {"stored": 6, "failed": 0}
    detail = a.get(f"/api/pacs/studies/{study_uid}", role="doctor").json()
    assert detail["study"]["NumberOfStudyRelatedInstances"] == 6
    assert detail["study"]["_ext"]["origin_facility"] == "2.25.222"
    # Bytes identical end to end.
    assert a.get(f"/api/pacs/studies/{study_uid}/verify").json()["ok"] == 6

    # B's audit trail names the peer facility, the practitioner and purpose.
    entries = b.get("/api/audit", params={"action_prefix": "pacs.", "limit": 200}).json()["entries"]
    peer_rows = [e for e in entries if (e["actor_name"] or "").startswith("peer:2.25.111")]
    assert {e["action"] for e in peer_rows} >= {"pacs.qido", "pacs.wado.retrieve"}
    assert any("doctor_hospa" in e["actor_name"] for e in peer_rows)
    assert any((e["detail"] or {}).get("purpose") == "TREAT" for e in peer_rows)


def test_dimse_query_and_move_between_hospitals(hospitals):
    a, b = hospitals
    study_uid, dsets = make_study(n_series=1, per_series=4, patient_name="Kazemi^Omid",
                                  patient_id="B-2002", accession="B-ACC-2", modality="MR")
    _stow(b, dsets)
    # B allows A's AE to query/retrieve and to be a move destination; A
    # registers B's DIMSE endpoint for federated search.
    b.post("/api/pacs/nodes", json={"name": "Hospital A PACS", "kind": "dimse",
                                    "ae_title": "HOSPA", "host": "127.0.0.1",
                                    "port": a.dimse_port, "is_move_destination": True})
    node = a.post("/api/pacs/nodes", json={"name": "Hospital B PACS (DIMSE)", "kind": "dimse",
                                           "ae_title": "HOSPB", "host": "127.0.0.1",
                                           "port": b.dimse_port, "federate": True}).json()
    assert a.post(f"/api/pacs/nodes/{node['id']}/echo").json() == {"ok": True}
    found = a.post(f"/api/pacs/nodes/{node['id']}/query",
                   json={"filters": {"PatientID": "B-2002"}}).json()["studies"]
    assert [s["StudyInstanceUID"] for s in found] == [study_uid]
    assert found[0]["ModalitiesInStudy"] == ["MR"]

    job = a.post("/api/pacs/retrieve", json={"study_uid": study_uid, "node_id": node["id"],
                                             "wait": True}).json()
    assert job["status"] == "done", job
    assert job["result"]["stored"] == 4  # C-MOVE sub-operations completed
    assert a.get(f"/api/pacs/studies/{study_uid}").json()["study"]["NumberOfStudyRelatedInstances"] == 4


def test_push_study_to_peer_and_bad_secret(hospitals):
    a, b = hospitals
    study_uid, dsets = make_study(n_series=1, per_series=2, patient_name="Sadeghi^Ali",
                                  patient_id="A-3003", accession="A-ACC-3")
    _stow(a, dsets)
    job = a.post("/api/pacs/send", json={"study_uid": study_uid, "node_id": "facility:2.25.222",
                                         "wait": True, "purpose": "TRANSFER"}).json()
    assert job["status"] == "done", job
    assert job["result"] == {"sent": 2, "failed": 0}
    assert b.get(f"/api/pacs/studies/{study_uid}").json()["study"]["_ext"]["origin_facility"] == "2.25.111"

    # A facility entry whose secret doesn't match B's: B refuses the token,
    # A's federated search reports the error instead of failing.
    rogue = b.facility_record(secret_env="PEER_SECRET_WRONG")
    rogue.update({"oid": "2.25.222", "name": "Hospital B"})
    a.post("/api/facilities", json=rogue)
    res = a.get("/api/pacs/studies", role="doctor", params={"q": "sadeghi", "scope": "all"}).json()
    assert any("401" in e["error"] for e in res["errors"]), res
    assert [s["StudyInstanceUID"] for s in res["studies"]] == [study_uid]  # local still answers
    a.post("/api/facilities", json=b.facility_record(secret_env="PEER_SECRET_B"))
    denied = b.get("/api/audit", params={"action_prefix": "peer.auth"}).json()["entries"]
    assert denied and denied[0]["outcome"] == "deny"


def test_orthanc_adapter_roundtrip(client, users, monkeypatch):
    from tests.functional.fake_orthanc import FakeOrthanc
    port = free_port()
    orthanc = FakeOrthanc(port).start()
    try:
        study_uid, dsets = make_study(n_series=2, per_series=2, patient_name="Tehrani^Sara",
                                      patient_id="OR-1", accession="OR-ACC", modality="CT")
        for d in dsets:
            orthanc.add(to_bytes(d))
        monkeypatch.setenv("ORTHANC_CRED", "orthanc:orthanc")
        admin = users["admin"]
        node = client.post("/api/pacs/nodes", headers=admin, json={
            "name": "Legacy Orthanc", "kind": "orthanc", "base_url": f"http://127.0.0.1:{port}",
            "auth_env": "ORTHANC_CRED", "federate": True}).json()
        assert node["has_credentials"] is True
        assert client.post(f"/api/pacs/nodes/{node['id']}/echo", headers=admin).json()["ok"]
        res = client.get("/api/pacs/studies", headers=users["doctor"],
                         params={"q": "tehrani", "scope": "remote"}).json()
        assert [s["StudyInstanceUID"] for s in res["studies"]] == [study_uid]
        assert res["studies"][0]["locations"][0]["name"] == "Legacy Orthanc"
        job = client.post("/api/pacs/retrieve", headers=admin,
                          json={"study_uid": study_uid, "node_id": node["id"], "wait": True}).json()
        assert job["result"] == {"stored": 4, "failed": 0}
        # Send a local-only study back to Orthanc.
        s2, d2 = make_study(n_series=1, per_series=2, patient_id="OR-2")
        for d in d2:
            from pacs import index
            index.ingest(to_bytes(d), source="test")
        job = client.post("/api/pacs/send", headers=admin,
                          json={"study_uid": s2, "node_id": node["id"], "wait": True}).json()
        assert job["result"] == {"sent": 2, "failed": 0}
        assert len(orthanc.instances) == 6
        # Wrong credentials -> retrieve job fails after retries, error recorded.
        monkeypatch.setenv("ORTHANC_CRED", "orthanc:wrong")
        monkeypatch.setenv("PACS_JOB_RETRY_BASE_SEC", "0.01")
        job = client.post("/api/pacs/retrieve", headers=admin,
                          json={"study_uid": study_uid, "node_id": node["id"], "wait": True}).json()
        assert job["status"] == "failed" and "401" in job["last_error"]
        assert job["attempts"] == 3
    finally:
        orthanc.stop()
