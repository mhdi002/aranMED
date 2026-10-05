"""Functional: the PACS accepts, indexes, serves and shows what real devices send.

Every object arrives over a real DICOM association (C-STORE to the running
SCP) in the transfer syntax the device would use. Each is then checked
through the same HTTP surface the viewer and other hospitals use:

1. **Real-world files** (pydicom's corpus from GE, Siemens, DCMTK, GDCM and
   dcm4che):
   - lossless and lossy JPEG, JPEG-LS, JPEG 2000 and RLE (8/16/32-bit);
   - uncompressed YBR, palette colour, Big Endian, Deflate;
   - Enhanced multi-frame, RT, SR and ECG.
2. **Synthetic devices** sent in many transfer syntaxes. Decoded pixels are
   compared with the originals exactly (lossless) or within JPEG tolerance.
   Colour is checked on a pure-red patch, which catches a double YBR
   conversion. Devices:
   - CT, MR, DX, MG, PT, NM (palette);
   - US (RGB and cine), XA (multi-frame);
   - Enhanced MR, SC (planar configuration 1);
   - SEG (1-bit), RT Dose (32-bit).
3. **Non-image objects**: SR text, Encapsulated PDF, a video stream and a
   vendor-private SOP class.
4. **Networking**:
   - C-MOVE and C-GET transcode for receivers that cannot take the stored
     compressed syntax, and C-GET via our own SCU receives everything;
   - worklist entries from HL7/FHIR (ISO dates) are found by modalities
     querying with DICOM dates;
   - Persian names round-trip through C-FIND, QIDO and MWL, including
     untagged legacy code pages;
   - TLS and mutual TLS on SCP and SCU;
   - limits: association count and called AE title;
   - duplicate and discontinued MPPS;
   - `/bulk` and WADO-URI;
   - DICOMDIR media and the zip-bomb cap.
"""
from __future__ import annotations

import datetime
import io
import os
import zipfile

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian, generate_uid
from pynetdicom import AE, ALL_TRANSFER_SYNTAXES, AllStoragePresentationContexts, build_role, evt
from pynetdicom.sop_class import (ModalityPerformedProcedureStep, ModalityWorklistInformationFind,
                                  StudyRootQueryRetrieveInformationModelFind,
                                  StudyRootQueryRetrieveInformationModelGet,
                                  StudyRootQueryRetrieveInformationModelMove, Verification)

from tests.functional import device_factory as F
from tests.functional.conftest import free_port

SCP_AE = "TESTPACS"


# --------------------------------------------------------------------------- fixtures
@pytest.fixture
def pacs(client, users):
    """The in-process hospital's SCP + registered modality / archive nodes."""
    from clinicaldb import facilities
    from pacs import dimse, nodes
    facilities.seed_local()
    ctx = {"archive_port": free_port(), "web": client, "h": users["admin"]}
    for name, ae, port in (("Device", "MODALITY", free_port()), ("LE-only archive", "LEARCH", ctx["archive_port"])):
        nodes.save({"name": name, "kind": "dimse", "ae_title": ae, "host": "127.0.0.1", "port": port,
                    "allow_store": True, "allow_query": True, "allow_retrieve": True,
                    "is_move_destination": True})
    server = dimse.DimseServer(ae_title=SCP_AE, bind="127.0.0.1", port=0).start()
    ctx["port"] = server.port
    nodes.save({"name": "Self", "kind": "dimse", "ae_title": SCP_AE, "host": "127.0.0.1",
                "port": server.port, "allow_store": True, "allow_query": True, "allow_retrieve": True,
                "is_move_destination": True})
    yield ctx
    server.stop()


def _ae(calling="MODALITY") -> AE:
    ae = AE(ae_title=calling)
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = 15
    return ae


def c_store(port: int, ds: Dataset, calling: str = "MODALITY") -> int:
    """C-STORE one object proposing exactly its own transfer syntax."""
    ae = _ae(calling)
    ae.add_requested_context(str(ds.SOPClassUID), [str(ds.file_meta.TransferSyntaxUID)])
    assoc = ae.associate("127.0.0.1", port, ae_title=SCP_AE)
    assert assoc.is_established
    try:
        return int(assoc.send_c_store(ds).Status)
    finally:
        assoc.release()


def stored(port: int, ds: Dataset) -> Dataset:
    ds = pydicom.dcmread(io.BytesIO(F.to_bytes(ds)))  # exactly what the device writes
    assert c_store(port, ds) == 0x0000
    return ds


def inst(sop: str) -> dict:
    from pacs import index
    rows = index.instance_paths(sop_uids=[sop])
    assert rows, f"{sop} not indexed"
    return rows[0]


def url(ds: Dataset, tail: str = "") -> str:
    return (f"/api/dicom-web/studies/{ds.StudyInstanceUID}/series/{ds.SeriesInstanceUID}"
            f"/instances/{ds.SOPInstanceUID}{tail}")


_DT = {"u8": np.uint8, "i8": np.int8, "u16": "<u2", "i16": "<i2", "u32": "<u4", "i32": "<i4", "f32": "<f4"}


def display(c, h, ds: Dataset, frame: int = 1) -> tuple[np.ndarray, dict]:
    r = c.get(url(ds, f"/frames/{frame}?normalize=1"), headers=h)
    assert r.status_code == 200, r.text
    info = {k.lower(): v for k, v in r.headers.items() if k.lower().startswith("x-")}
    arr = np.frombuffer(r.content, dtype=_DT[info["x-pixel-format"]])
    shape = (int(info["x-rows"]), int(info["x-columns"]))
    if info["x-samples-per-pixel"] == "3":
        shape += (3,)
    return arr.reshape(shape), info


def rendered(c, h, ds: Dataset, frame: int = 1) -> np.ndarray:
    from PIL import Image
    r = c.get(url(ds, f"/frames/{frame}/rendered"), headers={**h, "Accept": "image/png"})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    img = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"))
    assert img.std() > 1, "rendered image is blank"
    return img


def native_frames(c, h, ds: Dataset, frame: int = 1) -> bytes:
    from pacs import multipart
    r = c.get(url(ds, f"/frames/{frame}"),
              headers={**h, "Accept": 'multipart/related; type="application/octet-stream"; '
                                      'transfer-syntax=1.2.840.10008.1.2.1'})
    assert r.status_code == 200, r.text
    parts = list(multipart.decode(r.content, multipart.parse_boundary(r.headers["content-type"])))
    return parts[0][1]


def expected_display(src: Dataset, frame: int = 1) -> np.ndarray:
    """What the viewer must get, computed independently with pydicom."""
    from pydicom.pixels import apply_color_lut, pixel_array
    arr = pixel_array(src, index=frame - 1 if int(getattr(src, "NumberOfFrames", 1) or 1) > 1 else None)
    if str(src.PhotometricInterpretation) == "PALETTE COLOR":
        rgb = apply_color_lut(arr, src)
        bits = int(src.RedPaletteColorLookupTableDescriptor[2])
        return (rgb.astype(np.uint32) >> (bits - 8)).astype(np.uint8) if bits > 8 else rgb.astype(np.uint8)
    if arr.ndim == 3:
        bits = int(src.BitsStored)
        return arr.astype(np.uint8) if bits <= 8 else (arr.astype(np.uint64) >> (bits - 8)).astype(np.uint8)
    return arr


# --------------------------------------------------------------------------- 1. real-world files
@pytest.mark.parametrize("name", sorted(F.REAL_IMAGES))
def test_real_world_image_files(pacs, name):
    c, h = pacs["web"], pacs["h"]
    src = F.sample(name)
    ds = F.fresh_uids(src, patient_id=f"REAL-{abs(hash(name)) % 10**6}")
    assert c_store(pacs["port"], ds) == 0, F.REAL_IMAGES[name]
    row = inst(ds.SOPInstanceUID)
    assert row["sop_class_uid"] == str(src.SOPClassUID)
    assert row["transfer_syntax"] == str(src.file_meta.TransferSyntaxUID)
    n = int(getattr(src, "NumberOfFrames", 1) or 1)
    for frame in sorted({1, n}):
        got, info = display(c, h, ds, frame)
        want = expected_display(src, frame)
        assert got.shape == want.shape, (name, got.shape, want.shape)
        assert np.array_equal(got.astype(np.float64), want.astype(np.float64)), name
        rendered(c, h, ds, frame)
    if name.startswith("rtdose"):
        assert float(info["x-rescale-slope"]) == float(src.DoseGridScaling)


@pytest.mark.parametrize("name", sorted(F.REAL_MALFORMED))
def test_real_world_corrupt_streams_fail_cleanly(pacs, name):
    c, h = pacs["web"], pacs["h"]
    ds = F.fresh_uids(F.sample(name), patient_id="REAL-BAD")
    assert c_store(pacs["port"], ds) == 0, F.REAL_MALFORMED[name]       # archived as received
    for tail in ("/rendered", "/frames/1?normalize=1"):
        r = c.get(url(ds, tail), headers=h)
        assert r.status_code == 406 and "cannot decode" in r.json()["detail"], (tail, r.text[:200])
    # The stored object is still retrievable unchanged for other software.
    r = c.get(url(ds), headers={**h, "Accept": "application/dicom"})
    back = pydicom.dcmread(io.BytesIO(r.content))
    assert r.status_code == 200 and back.SOPInstanceUID == ds.SOPInstanceUID
    assert back.PixelData == ds.PixelData


@pytest.mark.parametrize("name", sorted(F.REAL_NON_IMAGES))
def test_real_world_non_image_files(pacs, name):
    c, h = pacs["web"], pacs["h"]
    ds = F.fresh_uids(F.sample(name), patient_id="REAL-NONIMG")
    assert c_store(pacs["port"], ds) == 0, F.REAL_NON_IMAGES[name]
    assert inst(ds.SOPInstanceUID)["sop_class_uid"] == str(ds.SOPClassUID)
    meta = c.get(url(ds, "/metadata"), headers=h)
    assert meta.status_code == 200 and meta.json()[0]["00080018"]["Value"] == [ds.SOPInstanceUID]
    r = c.get(url(ds, "/rendered"), headers=h)
    assert r.status_code in (404, 406), r.status_code  # no pixels: a clear refusal, never a 500
    if name == "test-SR.dcm":
        sr = c.get(url(ds, "/sr"), headers=h).json()
        assert sr["title"] and len(sr["text"].splitlines()) > 3


# --------------------------------------------------------------------------- 2. synthetic devices
CASES = [(k, s) for k, syns in F.DEVICE_SYNTAXES.items() for s in syns]


@pytest.mark.parametrize("kind,syntax", CASES, ids=[f"{k}-{s}" for k, s in CASES])
def test_device_in_transfer_syntax(pacs, kind, syntax):
    c, h = pacs["web"], pacs["h"]
    base = F.make_device(kind, patient_id=f"DEV-{kind}-{syntax}"[:16], patient_name="رضایی^مریم")
    ds = stored(pacs["port"], F.encode(base, syntax))
    row = inst(ds.SOPInstanceUID)
    n = int(getattr(base, "NumberOfFrames", 1) or 1)
    assert (row.get("frames") or 1) == n and row["transfer_syntax"] == str(F.SYNTAX_UID[syntax])

    for frame in sorted({1, n}):
        got, info = display(c, h, ds, frame)
        want = expected_display(base, frame)
        assert got.shape == want.shape
        if syntax in F.LOSSLESS:
            assert np.array_equal(got.astype(np.int64), want.astype(np.int64)), (kind, syntax, frame)
        else:
            assert np.abs(got.astype(int) - want.astype(int)).mean() < 8, (kind, syntax, frame)
        img = rendered(c, h, ds, frame)
        if kind in ("US", "US_CINE", "SC"):
            # The centre of the pure-red patch must render red: catches a second
            # YBR->RGB conversion (red turned green) or a dropped channel.
            r_, g_, b_ = (int(x) for x in img[img.shape[0] // 2, img.shape[1] // 2 - 6 + (frame - 1)])
            assert r_ > 180 and g_ < 80 and b_ < 80, (kind, syntax, (r_, g_, b_))
            assert info["x-photometric"] == "RGB"
        if kind == "NM":
            r_, g_, b_ = (int(x) for x in img[img.shape[0] // 2, img.shape[1] // 2])
            assert r_ > 200 and 90 < g_ < 170 and b_ < 40, (r_, g_, b_)   # palette applied
        if kind == "DX":
            assert info["x-photometric"] == "MONOCHROME1"
        if kind == "EMR":
            assert float(info["x-rescale-slope"]) == frame and float(info["x-rescale-intercept"]) == 10 * (frame - 1)
            assert float(info["x-window-center"]) == 1000.0
        if kind == "PT":
            assert float(info["x-rescale-slope"]) == 0.25
        if kind == "RTDOSE":
            assert float(info["x-rescale-slope"]) == 0.0001

    # Standard DICOMweb transcoding: native Explicit VR LE frames on request.
    if syntax in F.LOSSLESS and kind not in ("SEG",):
        from pydicom.pixels import pixel_array
        from pacs.render import frame_bytes
        raw = native_frames(c, h, ds)
        if syntax == "explicit":
            expect = frame_bytes(base, 1)          # already native: served as stored (planar kept)
        else:
            want = pixel_array(base, index=0 if n > 1 else None)
            expect = np.ascontiguousarray(want.astype(want.dtype.newbyteorder("<"))).tobytes()
        assert raw == expect, (kind, syntax)
    # As stored, uncompressed frames are byte-exact.
    if syntax == "explicit":
        from pacs import multipart
        r = c.get(url(ds, "/frames/1"), headers=h)
        part = list(multipart.decode(r.content, multipart.parse_boundary(r.headers["content-type"])))[0][1]
        from pacs.render import frame_bytes
        assert part == frame_bytes(base, 1)


def test_name_search_handles_persian_letters(pacs):
    c, h = pacs["web"], pacs["h"]
    ds = stored(pacs["port"], F.make_device("CT", patient_name="رضایی^علی", patient_id="FA-NAME-1"))
    for q in ("رضایی*", "رضايي*", "*علی*", "*علي*"):   # Persian and Arabic yeh/kaf variants
        hits = c.get("/api/dicom-web/studies", params={"PatientName": q}, headers=h).json()
        assert ds.StudyInstanceUID in [x["0020000D"]["Value"][0] for x in hits], q
    # C-FIND over DIMSE with UTF-8 returns the name intact.
    ae = _ae()
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
    assoc = ae.associate("127.0.0.1", pacs["port"], ae_title=SCP_AE)
    q = Dataset()
    q.SpecificCharacterSet = "ISO_IR 192"
    q.QueryRetrieveLevel, q.PatientName, q.StudyInstanceUID = "STUDY", "رضایی*", ""
    names = [str(i.PatientName) for s, i in assoc.send_c_find(q, StudyRootQueryRetrieveInformationModelFind)
             if s and s.Status == 0xFF00]
    assoc.release()
    assert "رضایی^علی" in names


def test_untagged_legacy_charset_and_arabic_sample_are_indexed(pacs):
    c, h = pacs["web"], pacs["h"]
    src = F.make_device("MR", patient_id="CP1256-1", charset=None)
    # Windows-1256 has the Arabic yeh (ي), which is what such devices write.
    name = "محمدي^زهرا".encode("cp1256")
    src.PatientName = "Q" * len(name)
    # The name as cp1256 bytes with no SpecificCharacterSet, as old devices send it.
    raw = F.to_bytes(src).replace(b"Q" * len(name), name)
    legacy = pydicom.dcmread(io.BytesIO(raw))
    assert c_store(pacs["port"], legacy) == 0
    hits = c.get("/api/dicom-web/studies", params={"PatientID": "CP1256-1"}, headers=h).json()
    assert hits and hits[0]["00100010"]["Value"][0]["Alphabetic"] == "محمدي^زهرا"
    # A Persian-keyboard search (Persian yeh) still finds it.
    found = c.get("/api/dicom-web/studies", params={"PatientName": "محمدی*"}, headers=h).json()
    assert any(x["00100020"]["Value"][0] == "CP1256-1" for x in found)
    arab = F.fresh_uids(F.sample("chrArab.dcm", charset_dir=True), patient_id="ARAB-1")
    assert c_store(pacs["port"], arab) == 0
    hits = c.get("/api/dicom-web/studies", params={"PatientID": "ARAB-1"}, headers=h).json()
    assert hits[0]["00100010"]["Value"][0]["Alphabetic"] == str(F.sample("chrArab.dcm", charset_dir=True).PatientName)


# --------------------------------------------------------------------------- 3. non-image objects
def test_sr_pdf_video_and_private_objects(pacs, monkeypatch):
    c, h = pacs["web"], pacs["h"]
    sr = stored(pacs["port"], F.make_device("SR", patient_id="OBJ-1"))
    out = c.get(url(sr, "/sr"), headers=h).json()
    assert out["title"] == "Diagnostic Imaging Report" and out["completion"] == "COMPLETE"
    assert "Impression: Pneumonia. یافته‌ها با پنومونی سازگار است." in out["text"]

    pdf_src = F.make_device("PDF", patient_id="OBJ-1")
    pdf = stored(pacs["port"], pdf_src)
    r = c.get(url(pdf, "/document"), headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and len(r.content) == int(pdf_src.EncapsulatedDocumentLength)

    vid_src = F.make_device("VIDEO", patient_id="OBJ-1")
    vid = stored(pacs["port"], vid_src)
    r = c.get(url(vid, "/video"), headers=h)
    from pydicom.encaps import generate_fragments
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert r.content == b"".join(generate_fragments(vid_src.PixelData))
    assert c.get(url(vid, "/rendered"), headers=h).status_code == 406   # not a 500
    assert c.get(url(vid, "/frames/1?normalize=1"), headers=h).status_code == 406

    # A vendor-private class is refused unless configured, then stored.
    priv = F.make_device("PRIVATE", patient_id="OBJ-1")
    with pytest.raises((AssertionError, ValueError, RuntimeError)):  # no acceptable context
        c_store(pacs["port"], priv)
    from pacs import dimse
    monkeypatch.setenv("PACS_EXTRA_SOP_CLASSES", F.PRIVATE_SOP)
    srv = dimse.DimseServer(ae_title=SCP_AE, bind="127.0.0.1", port=0).start()
    try:
        assert c_store(srv.port, priv) == 0
        assert inst(priv.SOPInstanceUID)["sop_class_uid"] == F.PRIVATE_SOP
    finally:
        srv.stop()


# --------------------------------------------------------------------------- 4. networking
class _LeOnlyArchive:
    """A workstation that only understands uncompressed Explicit VR LE."""

    def __init__(self, port: int):
        self.received: list[Dataset] = []
        ae = AE(ae_title="LEARCH")
        for cx in AllStoragePresentationContexts:
            ae.add_supported_context(cx.abstract_syntax, [ExplicitVRLittleEndian])

        def on_store(event):
            ds = event.dataset
            ds.file_meta = event.file_meta
            self.received.append(ds)
            return 0x0000
        self.server = ae.start_server(("127.0.0.1", port), block=False,
                                      evt_handlers=[(evt.EVT_C_STORE, on_store)])

    def stop(self):
        self.server.shutdown()


def test_move_and_get_transcode_for_receivers_without_compression(pacs):
    study = generate_uid()
    originals = {}
    for kind, syn in (("CT", "jpegls"), ("MR", "j2k"), ("US", "rle"), ("XA", "jpeg_lossless")):
        base = F.make_device(kind, study_uid=study, patient_id="MOVE-1")
        enc = stored(pacs["port"], F.encode(base, syn))
        originals[enc.SOPInstanceUID] = F.original_pixels(base)
    arch = _LeOnlyArchive(pacs["archive_port"])
    try:
        ae = _ae("MODALITY")
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelMove)
        assoc = ae.associate("127.0.0.1", pacs["port"], ae_title=SCP_AE)
        q = Dataset()
        q.QueryRetrieveLevel, q.StudyInstanceUID = "STUDY", study
        final = [s for s, _ in assoc.send_c_move(q, "LEARCH", StudyRootQueryRetrieveInformationModelMove) if s][-1]
        assoc.release()
        assert final.Status == 0x0000 and int(final.NumberOfCompletedSuboperations) == 4
        assert len(arch.received) == 4
        for ds in arch.received:
            assert str(ds.file_meta.TransferSyntaxUID) == ExplicitVRLittleEndian
            assert np.array_equal(ds.pixel_array, originals[ds.SOPInstanceUID])
    finally:
        arch.stop()

    # C-GET by a workstation that offers only Implicit VR LE: transcoded too.
    got: list[Dataset] = []

    def on_store(event):
        ds = event.dataset
        ds.file_meta = event.file_meta
        got.append(ds)
        return 0x0000
    ae = _ae("MODALITY")
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelGet)
    classes = sorted({str(F.make_device(k).SOPClassUID) for k in ("CT", "MR", "US", "XA")})
    for cl in classes:
        ae.add_requested_context(cl, [ImplicitVRLittleEndian])
    assoc = ae.associate("127.0.0.1", pacs["port"], ae_title=SCP_AE,
                         ext_neg=[build_role(cl, scp_role=True) for cl in classes],
                         evt_handlers=[(evt.EVT_C_STORE, on_store)])
    q = Dataset()
    q.QueryRetrieveLevel, q.StudyInstanceUID = "STUDY", study
    final = [s for s, _ in assoc.send_c_get(q, StudyRootQueryRetrieveInformationModelGet) if s][-1]
    assoc.release()
    assert final.Status == 0x0000 and len(got) == 4
    for ds in got:
        assert not ds.file_meta.TransferSyntaxUID.is_compressed
        assert np.array_equal(ds.pixel_array, originals[ds.SOPInstanceUID])

    # Our own SCU's C-GET asks for exactly the classes in the study, compressed allowed.
    from pacs import nodes, scu
    me = next(n for n in nodes.list_nodes() if n["ae_title"] == SCP_AE)
    res = scu.get(me, study_uid=study)
    assert res["stored"] == 4 and res["failed"] == 0


def test_scu_store_splits_associations_and_transcodes(pacs):
    from pacs import nodes, scu
    arch = _LeOnlyArchive(pacs["archive_port"])
    try:
        node = next(n for n in nodes.list_nodes() if n["ae_title"] == "LEARCH")
        dsets = [F.encode(F.make_device(k, patient_id="SCU-1"), s)
                 for k, s in (("CT", "jpegls"), ("MR", "rle"), ("US", "jpeg_baseline"), ("NM", "explicit"))]
        out = scu.store(node, [F.to_bytes(d) for d in dsets])
        assert out == {"sent": 4, "failed": 0}
        assert all(str(d.file_meta.TransferSyntaxUID) == ExplicitVRLittleEndian for d in arch.received)
    finally:
        arch.stop()


def test_worklist_from_orders_is_found_by_dicom_date_queries(pacs):
    from pacs import nodes, scu, worklist
    w = worklist.create({"modality": "MR", "patient_name": "کاظمی^سارا", "patient_id": "WL-ISO-1",
                         "scheduled_start": "2026-10-12T09:30:00Z", "procedure_description": "MRI KNEE"})
    assert w["scheduled_start"] == "20261012093000"
    worklist.update(w["id"], {"scheduled_start": "2026-10-12 10:15"})
    assert worklist.get(w["id"])["scheduled_start"] == "202610121015"
    me = next(n for n in nodes.list_nodes() if n["ae_title"] == SCP_AE)
    for date in ("20261012", "20261010-20261015", "-20261012", "20261012-"):
        found = scu.mwl_find(me, modality="MR", date=date)
        assert any(str(d.AccessionNumber) == w["accession"] for d in found), date
    found = scu.mwl_find(me, patient_name="کاظمی*")
    hit = next(d for d in found if str(d.AccessionNumber) == w["accession"])
    assert str(hit.PatientName) == "کاظمی^سارا"
    sps = hit.ScheduledProcedureStepSequence[0]
    assert (sps.ScheduledProcedureStepStartDate, sps.ScheduledProcedureStepStartTime) == ("20261012", "1015")
    assert worklist.list_entries(date="2026-10-12") and worklist.list_entries(date="20261012")


def test_mpps_duplicate_and_discontinued(pacs):
    from pacs import worklist
    w = worklist.create({"modality": "CT", "patient_id": "MPPS-9", "patient_name": "M^P"})
    ae = _ae()
    ae.add_requested_context(ModalityPerformedProcedureStep)
    assoc = ae.associate("127.0.0.1", pacs["port"], ae_title=SCP_AE)
    attrs = Dataset()
    attrs.PerformedProcedureStepStatus = "IN PROGRESS"
    attrs.Modality = "CT"
    ref = Dataset()
    ref.AccessionNumber, ref.StudyInstanceUID = w["accession"], w["study_uid"]
    attrs.ScheduledStepAttributesSequence = [ref]
    uid = generate_uid()
    st, _ = assoc.send_n_create(attrs, ModalityPerformedProcedureStep, uid)
    assert st.Status == 0x0000
    st, _ = assoc.send_n_create(attrs, ModalityPerformedProcedureStep, uid)
    assert st.Status == 0x0111                                     # duplicate SOP instance
    mod = Dataset()
    mod.PerformedProcedureStepStatus = "DISCONTINUED"
    st, _ = assoc.send_n_set(mod, ModalityPerformedProcedureStep, uid)
    assoc.release()
    assert st.Status == 0x0000
    assert worklist.get(w["id"])["status"] == "discontinued"


def _make_pki(tmp):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    now = datetime.datetime.now(datetime.timezone.utc)

    def key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def save(name, k, cert):
        (tmp / f"{name}.key").write_bytes(k.private_bytes(serialization.Encoding.PEM,
                                                           serialization.PrivateFormat.TraditionalOpenSSL,
                                                           serialization.NoEncryption()))
        (tmp / f"{name}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    ca_key = key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AranMed Test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now)
          .not_valid_after(now + datetime.timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(ca_key, hashes.SHA256()))
    save("ca", ca_key, ca)
    for name in ("server", "client"):
        k = key()
        cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
                .issuer_name(ca_name).public_key(k.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=2))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                .sign(ca_key, hashes.SHA256()))
        save(name, k, cert)


def test_dimse_tls_and_mutual_tls_on_scp_and_scu(pacs, tmp_path, monkeypatch):
    from pacs import dimse, nodes, scu
    _make_pki(tmp_path)
    monkeypatch.setenv("PACS_DIMSE_TLS_CERT", str(tmp_path / "server.crt"))
    monkeypatch.setenv("PACS_DIMSE_TLS_KEY", str(tmp_path / "server.key"))
    monkeypatch.setenv("PACS_DIMSE_TLS_CA", str(tmp_path / "ca.crt"))       # client certs required
    monkeypatch.setenv("T_CA", str(tmp_path / "ca.crt"))
    monkeypatch.setenv("T_CERT", str(tmp_path / "client.crt"))
    monkeypatch.setenv("T_KEY", str(tmp_path / "client.key"))
    srv = dimse.DimseServer(ae_title=SCP_AE, bind="127.0.0.1", port=0).start()
    try:
        tls_node = nodes.save({"name": "TLS self", "kind": "dimse", "ae_title": SCP_AE, "host": "127.0.0.1",
                               "port": srv.port, "tls": True, "tls_ca_env": "T_CA", "tls_cert_env": "T_CERT",
                               "tls_key_env": "T_KEY", "allow_store": True, "allow_query": True})
        assert scu.echo(tls_node) is True
        sent = scu.store(tls_node, [F.to_bytes(F.encode(F.make_device("CT", patient_id="TLS-1"), "jpegls"))])
        assert sent == {"sent": 1, "failed": 0}
        assert scu.find(tls_node, "STUDY", {"PatientID": "TLS-1"}, ["StudyInstanceUID"])
        no_client_cert = {**tls_node, "tls_cert_env": None, "tls_key_env": None}
        assert scu.echo(no_client_cert) is False                          # mTLS enforced
        plain = {**tls_node, "tls": False}
        assert scu.echo(plain) is False                                   # no plaintext on a TLS port
    finally:
        srv.stop()


def test_association_limits_and_called_ae(pacs, monkeypatch):
    from pacs import dimse
    ae = _ae()
    ae.add_requested_context(Verification)
    wrong = ae.associate("127.0.0.1", pacs["port"], ae_title="NOT_THE_PACS")
    assert not wrong.is_established
    stranger = _ae("STRANGER")
    stranger.add_requested_context(Verification)
    assert not stranger.associate("127.0.0.1", pacs["port"], ae_title=SCP_AE).is_established
    monkeypatch.setenv("PACS_MAX_ASSOCIATIONS", "1")
    srv = dimse.DimseServer(ae_title=SCP_AE, bind="127.0.0.1", port=0).start()
    try:
        first = ae.associate("127.0.0.1", srv.port, ae_title=SCP_AE)
        assert first.is_established
        second = ae.associate("127.0.0.1", srv.port, ae_title=SCP_AE)
        assert not second.is_established
        first.release()
    finally:
        srv.stop()


def test_bulk_wado_uri_and_media_import(pacs, monkeypatch):
    c, h = pacs["web"], pacs["h"]
    ds = stored(pacs["port"], F.make_device("CT", patient_id="URI-1", patient_name="Uri^Test"))
    from pacs import multipart
    r = c.get(url(ds, "/bulk/00100010"), headers=h)
    part = list(multipart.decode(r.content, multipart.parse_boundary(r.headers["content-type"])))[0][1]
    assert part.decode().strip() == "Uri^Test"
    q = {"requestType": "WADO", "studyUID": ds.StudyInstanceUID, "seriesUID": ds.SeriesInstanceUID,
         "objectUID": ds.SOPInstanceUID}
    r = c.get("/api/dicom-web/wado", params={**q, "contentType": "image/jpeg", "windowCenter": "40",
                                             "windowWidth": "400"}, headers=h)
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    assert c.get("/api/dicom-web/wado", params={**q, "contentType": "image/png", "frameNumber": "7"},
                 headers=h).status_code == 404
    vid = stored(pacs["port"], F.make_device("VIDEO", patient_id="URI-1"))
    r = c.get("/api/dicom-web/wado", params={"requestType": "WADO", "studyUID": vid.StudyInstanceUID,
                                             "seriesUID": vid.SeriesInstanceUID, "objectUID": vid.SOPInstanceUID},
              headers=h)
    assert r.status_code == 406

    # A CD/USB export: DICOMDIR + viewer files beside the images.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("DICOMDIR", b"\x00" * 200)
        z.writestr("VIEWER/RUN.EXE", b"MZ...")
        for i, kind in enumerate(("CR", "MR")):
            src = F.make_device("CT" if kind == "CR" else "MR", patient_id="MEDIA-1")
            z.writestr(f"DICOM/IMG{i:04d}", F.to_bytes(src))
    r = c.post("/api/pacs/upload", headers=h, files=[("files", ("cd.zip", buf.getvalue(), "application/zip"))])
    body = r.json()
    assert body["stored"] == 2 and body["failed"] == [] and len(body["skipped"]) == 2
    # A zip bomb (tiny archive, huge content) is refused before unpacking.
    monkeypatch.setenv("PACS_MAX_UPLOAD_MB", "1")
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.dcm", b"\x00" * (5 * 1024 * 1024))
    r = c.post("/api/pacs/upload", headers=h, files=[("files", ("bomb.zip", bomb.getvalue(), "application/zip"))])
    assert r.status_code == 413


def test_other_hospitals_pacs_sees_only_consented_patients_over_dimse(pacs):
    """A DICOM node that belongs to another hospital (facility_oid set) gets
    the same consent decision over C-FIND / C-MOVE as over DICOMweb/FHIR."""
    from ehr import store
    from pacs import index, nodes
    nodes.save({"name": "Peer hospital PACS", "kind": "dimse", "ae_title": "PEERPACS", "host": "127.0.0.1",
                "port": pacs["archive_port"], "facility_oid": "2.25.4242", "allow_query": True,
                "allow_retrieve": True, "is_move_destination": True})
    shared = stored(pacs["port"], F.make_device("CT", patient_id="CONSENT-OK", patient_name="Ok^Patient"))
    private = stored(pacs["port"], F.make_device("CT", patient_id="CONSENT-NO", patient_name="No^Patient"))
    person = index.get_study(private.StudyInstanceUID)["_ext"]["person_id"]
    store.create("consent", {"person_id": person, "category": "deny-sharing", "grantee": "2.25.4242",
                             "status": "active"})

    def find(calling):
        ae = _ae(calling)
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
        assoc = ae.associate("127.0.0.1", pacs["port"], ae_title=SCP_AE)
        q = Dataset()
        q.QueryRetrieveLevel, q.StudyInstanceUID, q.PatientID = "STUDY", "", "CONSENT-*"
        out = [str(i.StudyInstanceUID) for s, i in assoc.send_c_find(q, StudyRootQueryRetrieveInformationModelFind)
               if s and s.Status == 0xFF00]
        assoc.release()
        return set(out)
    assert find("MODALITY") == {shared.StudyInstanceUID, private.StudyInstanceUID}   # own device: all
    assert find("PEERPACS") == {shared.StudyInstanceUID}                              # peer: consented only

    ae = _ae("PEERPACS")
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelMove)
    assoc = ae.associate("127.0.0.1", pacs["port"], ae_title=SCP_AE)
    q = Dataset()
    q.QueryRetrieveLevel, q.StudyInstanceUID = "STUDY", private.StudyInstanceUID
    final = [s for s, _ in assoc.send_c_move(q, "PEERPACS", StudyRootQueryRetrieveInformationModelMove) if s][-1]
    assoc.release()
    assert int(final.get("NumberOfCompletedSuboperations", 0) or 0) == 0
