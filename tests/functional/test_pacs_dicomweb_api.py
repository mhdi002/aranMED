"""Functional: DICOMweb (STOW/QIDO/WADO) + the /api/pacs management surface.

Uses the real app over HTTP (TestClient), with real DICOM payloads. Covers
the viewer's data path (metadata -> frames -> rendered) as well as upload,
reports, worklist and permissions.
"""
from __future__ import annotations

import io
import zipfile

import numpy as np
import pydicom
from PIL import Image

from pacs import multipart
from tests.functional.dicom_factory import make_instance, make_study, to_bytes
from pydicom.uid import generate_uid

DW = "/api/dicom-web"


def _stow(client, headers, dsets, study=None):
    bnd = multipart.boundary()
    body = multipart.encode(((to_bytes(d), "application/dicom") for d in dsets), bnd)
    return client.post(f"{DW}/studies" + (f"/{study}" if study else ""), content=body,
                       headers={**headers,
                                "Content-Type": multipart.content_type(bnd, "application/dicom"),
                                "Accept": "application/dicom+json"})


def _kw(js, tag):
    v = js.get(tag, {}).get("Value")
    return v[0] if v else None


def test_stow_qido_wado_viewer_path(client, users):
    rad = users["radiologist"]
    study_uid, dsets = make_study(n_series=2, per_series=3, patient_name="Jafari^Leila",
                                  patient_id="DW-1", accession="DW-ACC", study_date="20260920")
    # Multi-frame object in its own series.
    mf_series = generate_uid()
    mf = make_instance(study_uid=study_uid, series_uid=mf_series, frames=4, series_number=9,
                       patient_name="Jafari^Leila", patient_id="DW-1", accession="DW-ACC")
    dsets.append(mf)

    # --- STOW-RS ----------------------------------------------------------------
    r = _stow(client, rad, dsets)
    assert r.status_code == 200, r.text
    resp = r.json()
    assert len(resp["00081199"]["Value"]) == 7
    assert "00081198" not in resp

    # STOW into the wrong study is rejected per instance (409 when all fail).
    other_uid, other = make_study(n_series=1, per_series=1)
    r = _stow(client, rad, other, study=study_uid)
    assert r.status_code == 409
    assert r.json()["00081198"]["Value"][0]["00081197"]["Value"][0] == 0xA900

    # Students cannot store; doctors can read but STOW needs pacs.write (they have it).
    assert _stow(client, users["student"], other).status_code == 403

    # --- QIDO-RS --------------------------------------------------------------
    r = client.get(f"{DW}/studies", params={"PatientName": "jafari*",
                                            "StudyDate": "20260901-20260930"}, headers=rad)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/dicom+json")
    studies = r.json()
    assert len(studies) == 1
    s = studies[0]
    assert _kw(s, "0020000D") == study_uid
    assert _kw(s, "00201208") == 7  # NumberOfStudyRelatedInstances
    assert s["00100010"]["Value"][0]["Alphabetic"] == "Jafari^Leila"
    assert _kw(s, "00081190").endswith(f"{DW}/studies/{study_uid}")
    # Tag-number keys and includefield work too.
    r = client.get(f"{DW}/studies", params={"00100020": "DW-1", "includefield": "00081030"},
                   headers=rad)
    assert "00081030" in r.json()[0]
    assert client.get(f"{DW}/studies", params={"PatientID": "nobody"}, headers=rad).json() == []

    series = client.get(f"{DW}/studies/{study_uid}/series", headers=rad).json()
    assert len(series) == 3
    se_uid = next(_kw(x, "0020000E") for x in series if _kw(x, "00200011") == 1)
    inst = client.get(f"{DW}/studies/{study_uid}/series/{se_uid}/instances", headers=rad).json()
    assert len(inst) == 3
    assert client.get(f"{DW}/series", params={"Modality": "CT", "PatientID": "DW-1"},
                      headers=rad).json()
    assert len(client.get(f"{DW}/studies/{study_uid}/instances", headers=rad).json()) == 7

    # --- WADO-RS metadata -> frames (what the browser viewer does) --------------
    meta = client.get(f"{DW}/studies/{study_uid}/series/{se_uid}/metadata", headers=rad).json()
    assert len(meta) == 3
    m0 = meta[0]
    assert _kw(m0, "00280010") == 64 and _kw(m0, "00280011") == 64  # Rows/Columns
    assert m0["7FE00010"]["BulkDataURI"].endswith("/frames/1")
    assert _kw(m0, "00020010") == "1.2.840.10008.1.2.1"
    sop = _kw(m0, "00080018")
    r = client.get(f"{DW}/studies/{study_uid}/series/{se_uid}/instances/{sop}/frames/1",
                   headers=rad)
    assert r.status_code == 200
    assert 'type="application/octet-stream"' in r.headers["content-type"]
    parts = multipart.decode(r.content, multipart.parse_boundary(r.headers["content-type"]))
    assert len(parts) == 1
    original = next(d for d in dsets if d.SOPInstanceUID == sop)
    assert parts[0][1] == original.PixelData  # byte-identical native frame

    # Multi-frame: frames 2,4 in one request, each the right slice.
    r = client.get(f"{DW}/studies/{study_uid}/series/{mf_series}/instances/"
                   f"{mf.SOPInstanceUID}/frames/2,4", headers=rad)
    parts = multipart.decode(r.content, multipart.parse_boundary(r.headers["content-type"]))
    size = 64 * 64 * 2
    assert [p[1] for p in parts] == [mf.PixelData[size:2 * size], mf.PixelData[3 * size:4 * size]]
    assert client.get(f"{DW}/studies/{study_uid}/series/{mf_series}/instances/"
                      f"{mf.SOPInstanceUID}/frames/9", headers=rad).status_code == 404

    # --- rendered / thumbnail: real images with windowing applied ---------------
    url = f"{DW}/studies/{study_uid}/series/{se_uid}/instances/{sop}/rendered"
    png = client.get(url, headers=rad)
    assert png.headers["content-type"] == "image/png"
    img = np.asarray(Image.open(io.BytesIO(png.content)))
    assert img.shape == (64, 64) and img.std() > 10  # not a blank image
    narrow = np.asarray(Image.open(io.BytesIO(
        client.get(url, params={"window": "-800,400"}, headers=rad).content)))
    # A lung-ish window reveals the background ramp the soft-tissue window hides.
    assert not np.array_equal(img, narrow) and narrow[:, :8].std() > img[:, :8].std()
    jpg = client.get(url, headers={**rad, "Accept": "image/jpeg"})
    assert jpg.headers["content-type"] == "image/jpeg"
    th = client.get(f"{DW}/studies/{study_uid}/thumbnail", params={"size": 48}, headers=rad)
    assert max(Image.open(io.BytesIO(th.content)).size) <= 64

    # --- WADO-RS retrieve + WADO-URI --------------------------------------------
    r = client.get(f"{DW}/studies/{study_uid}/series/{se_uid}", headers=rad)
    parts = multipart.decode(r.content, multipart.parse_boundary(r.headers["content-type"]))
    assert len(parts) == 3
    back = pydicom.dcmread(io.BytesIO(parts[0][1]))
    assert back.StudyInstanceUID == study_uid
    r = client.get(f"{DW}/wado", params={"requestType": "WADO", "studyUID": study_uid,
                                         "seriesUID": se_uid, "objectUID": sop,
                                         "contentType": "application/dicom"}, headers=rad)
    assert pydicom.dcmread(io.BytesIO(r.content)).SOPInstanceUID == sop

    # Unauthenticated access is refused everywhere.
    assert client.get(f"{DW}/studies").status_code == 401


def test_pacs_api_upload_browse_report_worklist(client, users):
    doc, rad, admin = users["doctor"], users["radiologist"], users["admin"]
    study_uid, dsets = make_study(n_series=1, per_series=3, patient_name="Hosseini^Amir",
                                  patient_id="UP-9", accession="UP-ACC", modality="MR",
                                  study_desc="MRI KNEE", body_part="KNEE")
    # Zip upload + a loose file + a non-DICOM file.
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        for i, d in enumerate(dsets[:2]):
            z.writestr(f"img{i}.dcm", to_bytes(d))
    files = [("files", ("study.zip", zbuf.getvalue(), "application/zip")),
             ("files", ("x.dcm", to_bytes(dsets[2]), "application/dicom")),
             ("files", ("notes.txt", b"hello", "text/plain"))]
    r = client.post("/api/pacs/upload", files=files, headers=rad)
    assert r.status_code == 200, r.text
    up = r.json()
    assert up["stored"] == 3 and len(up["failed"]) == 1
    assert up["studies"][0]["StudyInstanceUID"] == study_uid

    # Browse with friendly filters.
    r = client.get("/api/pacs/studies", params={"q": "hosseini", "modality": "MR"}, headers=doc)
    assert r.json()["total"] == 1
    assert client.get("/api/pacs/studies", params={"modality": "CT", "q": "hosseini"},
                      headers=doc).json()["total"] == 0
    detail = client.get(f"/api/pacs/studies/{study_uid}", headers=doc).json()
    assert detail["study"]["ModalitiesInStudy"] == ["MR"]
    assert len(detail["series"][0]["instances"]) == 3
    assert detail["study"]["_ext"]["body_parts"] == ["KNEE"]

    # Report lifecycle: draft -> final -> study 'reported'; final can only be amended.
    r = client.post(f"/api/pacs/studies/{study_uid}/reports", headers=rad,
                    json={"text": "Draft: small effusion.", "status": "draft"})
    rep = r.json()
    assert rep["status"] == "draft"
    r = client.post(f"/api/pacs/studies/{study_uid}/reports", headers=rad,
                    json={"text": "Small joint effusion. No tear.", "status": "final",
                          "report_id": rep["id"]})
    assert r.json()["status"] == "final"
    assert client.get(f"/api/pacs/studies/{study_uid}", headers=doc).json()["study"]["_ext"]["status"] == "reported"
    r = client.post(f"/api/pacs/studies/{study_uid}/reports", headers=rad,
                    json={"text": "x", "status": "draft", "report_id": rep["id"]})
    assert r.status_code == 400
    # Residents can read but not write reports into the PACS.
    assert client.post(f"/api/pacs/studies/{study_uid}/reports", headers=users["resident"],
                       json={"text": "x"}).status_code == 403

    # Worklist via API.
    r = client.post("/api/pacs/worklist", headers=doc, json={
        "modality": "CT", "patient_name": "Hosseini^Amir", "patient_id": "UP-9",
        "procedure_description": "CT Abdomen", "scheduled_start": "20261004T080000"})
    wl = r.json()
    assert wl["status"] == "scheduled" and wl["study_uid"]
    # Same MPI person as the uploaded study (same PatientID + issuer).
    assert wl["person_id"] == detail["study"]["_ext"]["person_id"]
    r = client.patch(f"/api/pacs/worklist/{wl['id']}", headers=doc, json={"status": "cancelled"})
    assert r.json()["status"] == "cancelled"
    assert client.patch(f"/api/pacs/worklist/{wl['id']}", headers=doc,
                        json={"status": "bogus"}).status_code == 400

    # Stats, integrity, admin-only delete.
    st = client.get("/api/pacs/stats", headers=doc).json()
    assert st["studies"] == 1 and st["by_modality"]["MR"] == 1
    assert client.get(f"/api/pacs/studies/{study_uid}/verify", headers=doc).json()["ok"] == 3
    assert client.delete(f"/api/pacs/studies/{study_uid}", headers=rad).status_code == 403
    assert client.delete(f"/api/pacs/studies/{study_uid}", headers=admin).json()["instances"] == 3
    assert client.get(f"/api/pacs/studies/{study_uid}", headers=doc).status_code == 404


def test_node_admin_and_validation(client, users):
    admin = users["admin"]
    bad = client.post("/api/pacs/nodes", headers=admin, json={"name": "x", "kind": "dimse"})
    assert bad.status_code == 400
    n = client.post("/api/pacs/nodes", headers=admin, json={
        "name": "Ortho PACS", "kind": "orthanc", "base_url": "http://127.0.0.1:1",
        "auth_env": "ORTHO_CRED", "federate": True}).json()
    assert n["federate"] is True and n["has_credentials"] is False
    assert client.post("/api/pacs/nodes", headers=users["doctor"],
                       json={"name": "y", "kind": "dicomweb", "base_url": "http://x"}).status_code == 403
    listing = client.get("/api/pacs/nodes", headers=users["doctor"]).json()
    assert any(t["name"] == "Ortho PACS" for t in listing["federation_targets"])
    # Unreachable node: echo reports false, federated search degrades with an error.
    assert client.post(f"/api/pacs/nodes/{n['id']}/echo", headers=admin).json() == {"ok": False}
    res = client.get("/api/pacs/studies", params={"scope": "all", "q": "x"},
                     headers=users["doctor"]).json()
    assert res["errors"] and res["errors"][0]["node"] == "Ortho PACS"
    assert client.delete(f"/api/pacs/nodes/{n['id']}", headers=admin).status_code == 200
