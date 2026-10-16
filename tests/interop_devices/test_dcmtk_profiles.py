"""Vendor device profiles emulated with dcmtk against AranMed's SCP.

The behaviour of each profile is taken from the vendor's published DICOM
conformance statement: which transfer syntaxes the device proposes, its
PDU size, how it uses associations, what it queries the worklist with.
"""
from __future__ import annotations

from pathlib import Path

import pydicom
import pytest
from pydicom.uid import generate_uid

from tests.functional import device_factory as F
from tests.interop_devices.conftest import SCP_AE, run

pytestmark = pytest.mark.interop

US_MF_RETIRED = "1.2.840.10008.5.1.4.1.1.3"
BTO = "1.2.840.10008.5.1.4.1.1.13.1.3"
BREAST_PROJ = "1.2.840.10008.5.1.4.1.1.13.1.4"
HOLOGIC_PRIVATE = "1.2.840.113681.1.2.1"


def _write(tmp: Path, name: str, ds) -> Path:
    p = tmp / name
    p.write_bytes(F.to_bytes(ds))
    return p


def _storescu(scp, aet: str, files: list[Path], *flags: str) -> None:
    r = run(["storescu", "-aet", aet, "-aec", SCP_AE, *flags, "-R", "127.0.0.1",
             str(scp["port"]), *map(str, files)])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Store SCU RQ" in r.stdout + r.stderr or r.returncode == 0


def _stored(*sop_uids) -> set[str]:
    from pacs import index
    return {i["sop_uid"] for i in index.instance_paths(sop_uids=list(sop_uids))}


def _renders(sop_uid: str) -> bool:
    from pacs import index, render
    from pacs.storage import get_storage
    inst = index.instance_paths(sop_uids=[sop_uid])[0]
    out = render.render(get_storage().get(inst["path"]), frame=1)
    return bool(out)


# ---------------------------------------------------------------- verification
@pytest.mark.device(vendor="All devices", service="C-ECHO (Verification)")
def test_echo_known_device_and_rejections(scp):
    ok = run(["echoscu", "-aet", "SIEMENS_CT", "-aec", SCP_AE, "127.0.0.1", str(scp["port"])])
    assert ok.returncode == 0, ok.stderr
    stranger = run(["echoscu", "-aet", "UNKNOWN_DEV", "-aec", SCP_AE, "127.0.0.1", str(scp["port"])])
    assert stranger.returncode != 0 and "Rejected" in stranger.stderr + stranger.stdout
    wrong_called = run(["echoscu", "-aet", "SIEMENS_CT", "-aec", "NOT_PACS", "127.0.0.1",
                        str(scp["port"])])
    assert wrong_called.returncode != 0


# ---------------------------------------------------------------- Siemens CT
@pytest.mark.device(vendor="Siemens SOMATOM (syngo CT)", service="C-STORE JPEG Lossless SV1, PDU 32 KB")
def test_siemens_ct_jpeg_lossless(scp):
    study = generate_uid()
    files, sops = [], []
    for i in range(3):
        ds = F.encode(F.make_device("CT", study_uid=study, patient_id="SIE-1"), "jpeg_lossless")
        files.append(_write(scp["tmp"], f"sie{i}.dcm", ds))
        sops.append(ds.SOPInstanceUID)
    _storescu(scp, "SIEMENS_CT", files, "-xs", "-pdu", "32768")
    assert _stored(*sops) == set(sops)
    assert _renders(sops[0])


@pytest.mark.device(vendor="Siemens SOMATOM (syngo CT)", service="C-STORE Enhanced MR/CT multi-frame, J2K, Dose SR")
def test_siemens_enhanced_and_dose_sr(scp):
    emr = F.encode(F.make_device("EMR", patient_id="SIE-2"), "j2k")
    sr = F.make_device("SR", patient_id="SIE-2")
    files = [_write(scp["tmp"], "emr.dcm", emr), _write(scp["tmp"], "sr.dcm", sr)]
    _storescu(scp, "SIEMENS_CT", files[:1], "-xv")
    _storescu(scp, "SIEMENS_CT", files[1:])
    assert _stored(emr.SOPInstanceUID, sr.SOPInstanceUID) == {emr.SOPInstanceUID, sr.SOPInstanceUID}


# ---------------------------------------------------------------- Canon CT
@pytest.mark.device(vendor="Canon Aquilion ONE", service="C-STORE JPEG Lossless only, PDU 16 KB")
def test_canon_small_pdu_jpeg_lossless(scp):
    ds = F.encode(F.make_device("CT", patient_id="CAN-1"), "jpeg_lossless")
    f = _write(scp["tmp"], "canon.dcm", ds)
    _storescu(scp, "CANON_CT", [f], "-xs", "-pdu", "16384", "--max-send-pdu", "16384")
    assert _stored(ds.SOPInstanceUID)


@pytest.mark.device(vendor="Canon Aquilion ONE", service="MWL C-FIND (Latin-1 console, Persian patient)")
def test_canon_worklist_latin1(scp):
    from pacs import worklist
    worklist.create({"modality": "CT", "patient_id": "CAN-2", "patient_name": "زهرا^کریمی",
                     "station_ae": "CANON_CT", "scheduled_start": "2026-10-16T08:30:00",
                     "procedure_description": "CT CHEST"})
    out = scp["tmp"] / "mwl"
    out.mkdir()
    r = run(["findscu", "-W", "-aet", "CANON_CT", "-aec", SCP_AE, "-X", "-od", str(out),
             "-k", "PatientName", "-k", "PatientID", "-k", "SpecificCharacterSet",
             "-k", "ScheduledProcedureStepSequence[0].ScheduledStationAETitle=CANON_CT",
             "-k", "ScheduledProcedureStepSequence[0].ScheduledProcedureStepStartDate=20261016-20261016",
             "-k", "ScheduledProcedureStepSequence[0].Modality=CT",
             "127.0.0.1", str(scp["port"])])
    assert r.returncode == 0, r.stderr
    rsp = sorted(out.glob("rsp*.dcm"))
    assert len(rsp) == 1
    ds = pydicom.dcmread(rsp[0])
    assert ds.SpecificCharacterSet == "ISO_IR 100"
    assert str(ds.PatientName) == "Zahra^Karimi"


# ---------------------------------------------------------------- Philips EPIQ ultrasound
@pytest.mark.device(vendor="Philips EPIQ / Affiniti", service="C-STORE US Multi-frame JPEG Baseline")
def test_epiq_us_cine_jpeg_baseline(scp):
    ds = F.encode(F.make_device("US_CINE", patient_id="EPQ-1"), "jpeg_baseline")
    f = _write(scp["tmp"], "cine.dcm", ds)
    _storescu(scp, "EPIQ_US", [f], "-xy")
    assert _stored(ds.SOPInstanceUID)
    assert _renders(ds.SOPInstanceUID)


@pytest.mark.device(vendor="Philips EPIQ / Affiniti", service="Implicit-VR-only mode, retired US Multi-frame class, one association per image")
def test_epiq_implicit_only_retired_class(scp):
    sops = []
    for i in range(3):
        ds = F.make_device("US", patient_id="EPQ-2")
        ds.SOPClassUID = US_MF_RETIRED
        ds.file_meta.MediaStorageSOPClassUID = US_MF_RETIRED
        f = _write(scp["tmp"], f"epiq{i}.dcm", ds)
        _storescu(scp, "EPIQ_US", [f], "-xi")              # a new association for every image
        sops.append(ds.SOPInstanceUID)
    assert _stored(*sops) == set(sops)


# ---------------------------------------------------------------- Hologic mammography
_HOLOGIC_CFG = """\
[[TransferSyntaxes]]
[Lossless]
TransferSyntax1 = 1.2.840.10008.1.2.4.70
TransferSyntax2 = 1.2.840.10008.1.2.1
TransferSyntax3 = 1.2.840.10008.1.2

[[PresentationContexts]]
[HologicContexts]
PresentationContext1 = %s\\Lossless
PresentationContext2 = %s\\Lossless
PresentationContext3 = %s\\Lossless
PresentationContext4 = 1.2.840.10008.5.1.4.1.1.1.2\\Lossless
PresentationContext5 = 1.2.840.10008.5.1.4.1.1.1.2.1\\Lossless

[[Profiles]]
[Hologic]
PresentationContexts = HologicContexts
""" % (BTO, BREAST_PROJ, HOLOGIC_PRIVATE)


@pytest.mark.device(vendor="Hologic Selenia Dimensions", service="C-STORE Breast Tomosynthesis, Breast Projection, MG For Processing, vendor-private class")
def test_hologic_tomo_projection_and_private(scp):
    files, sops = [], []
    for sop_class, name in ((BTO, "bto"), (BREAST_PROJ, "proj"), (HOLOGIC_PRIVATE, "pvt")):
        ds = F.encode(F.make_device("MG", patient_id="HOL-1"), "jpeg_lossless")
        ds.SOPClassUID = sop_class
        ds.file_meta.MediaStorageSOPClassUID = sop_class
        files.append(_write(scp["tmp"], f"{name}.dcm", ds))
        sops.append(ds.SOPInstanceUID)
    mg = F.encode(F.make_device("MG", patient_id="HOL-1"), "jpeg_lossless")
    files.append(_write(scp["tmp"], "mg.dcm", mg))
    sops.append(mg.SOPInstanceUID)
    # A configured profile, as a modality has: dcmtk only sends a
    # vendor-private class when it is listed explicitly.
    cfg = scp["tmp"] / "hologic.cfg"
    cfg.write_text(_HOLOGIC_CFG)
    r = run(["storescu", "-aet", "HOLOGIC_MG", "-aec", SCP_AE, "-xf", str(cfg), "Hologic",
             "127.0.0.1", str(scp["port"]), *map(str, files)])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _stored(*sops) == set(sops)


# ---------------------------------------------------------------- CR / DR
@pytest.mark.device(vendor="Carestream DirectView CR / DRX", service="C-STORE CR, DX For Presentation, PDU 128 KB")
def test_carestream_cr_dx(scp):
    from tests.functional.dicom_factory import make_instance
    cr = make_instance(study_uid=generate_uid(), series_uid=generate_uid(), modality="CR",
                       patient_id="CR-1")
    dx = F.make_device("DX", patient_id="CR-1")
    files = [_write(scp["tmp"], "cr.dcm", cr), _write(scp["tmp"], "dx.dcm", dx)]
    _storescu(scp, "CR_ROOM", files, "-pdu", "131072")
    assert _stored(cr.SOPInstanceUID, dx.SOPInstanceUID) == {cr.SOPInstanceUID, dx.SOPInstanceUID}


# ---------------------------------------------------------------- Q/R from a workstation
@pytest.mark.device(vendor="Reading workstation", service="C-FIND / C-MOVE / C-GET (Study Root)")
def test_console_query_move_get(scp):
    import subprocess
    import time
    study = generate_uid()
    sops, files = [], []
    for i in range(4):
        ds = F.make_device("CT", study_uid=study, patient_id="QR-1")
        files.append(_write(scp["tmp"], f"qr{i}.dcm", ds))
        sops.append(ds.SOPInstanceUID)
    _storescu(scp, "SIEMENS_CT", files)

    found = scp["tmp"] / "found"
    found.mkdir()
    r = run(["findscu", "-S", "-aet", "CONSOLE", "-aec", SCP_AE, "-X", "-od", str(found),
             "-k", "QueryRetrieveLevel=STUDY", "-k", "PatientID=QR-1", "-k", "StudyInstanceUID",
             "-k", "NumberOfStudyRelatedInstances", "127.0.0.1", str(scp["port"])])
    assert r.returncode == 0, r.stderr
    rsp = [pydicom.dcmread(p) for p in found.glob("rsp*.dcm")]
    assert [str(d.StudyInstanceUID) for d in rsp] == [study]

    moved = scp["tmp"] / "moved"
    moved.mkdir()
    receiver = subprocess.Popen(["storescp", "-aet", "CONSOLE", "-od", str(moved),
                                 str(scp["console_port"])],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(0.5)
        r = run(["movescu", "-S", "-aet", "CONSOLE", "-aec", SCP_AE, "-aem", "CONSOLE",
                 "-k", "QueryRetrieveLevel=STUDY", "-k", f"StudyInstanceUID={study}",
                 "127.0.0.1", str(scp["port"])])
        assert r.returncode == 0, r.stderr
        deadline = time.time() + 10
        while len(list(moved.iterdir())) < 4 and time.time() < deadline:
            time.sleep(0.1)
        got = {str(pydicom.dcmread(p).SOPInstanceUID) for p in moved.iterdir()}
        assert got == set(sops)
    finally:
        receiver.terminate()

    pulled = scp["tmp"] / "pulled"
    pulled.mkdir()
    r = run(["getscu", "-S", "-aet", "CONSOLE", "-aec", SCP_AE, "-od", str(pulled),
             "-k", "QueryRetrieveLevel=STUDY", "-k", f"StudyInstanceUID={study}",
             "127.0.0.1", str(scp["port"])])
    assert r.returncode == 0, r.stderr
    assert {str(pydicom.dcmread(p).SOPInstanceUID) for p in pulled.iterdir()} == set(sops)


# ---------------------------------------------------------------- character sets
@pytest.mark.device(vendor="Multi-language devices", service="Specific Character Set (ISO 2022, Arabic, Greek, Hebrew, Cyrillic, Latin-1)")
def test_character_set_files(scp):
    from pacs import index
    files = sorted(p for p in F.CHARSETS.glob("chr*.dcm"))
    expected = {}
    for p in files:
        ds = F.fresh_uids(pydicom.dcmread(p), patient_id=f"CS-{p.stem}")
        out = scp["tmp"] / p.name
        ds.save_as(out, enforce_file_format=True)
        if "PatientName" not in ds:
            continue
        expected[f"CS-{p.stem}"] = str(ds.PatientName)
        _storescu(scp, "CORPUS", [out])
    for pid, name in expected.items():
        hits = index.query_studies({"PatientID": pid})
        assert hits, pid
        assert hits[0]["PatientName"] == name, (pid, hits[0]["PatientName"], name)


# ---------------------------------------------------------------- video (endoscopy / US carts)
@pytest.mark.device(vendor="Endoscopy tower / ultrasound cart", service="C-STORE MPEG-4 AVC/H.264 video; playable MP4 on retrieval")
def test_endoscopy_h264_video(scp):
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    from pydicom.encaps import encapsulate
    from pacs import index, render
    from pacs.storage import get_storage
    stream = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                             "-i", "testsrc=size=64x64:rate=10", "-t", "1", "-c:v", "libx264",
                             "-pix_fmt", "yuv420p", "-f", "h264", "-"],
                            capture_output=True, check=True).stdout
    ds = F.make_device("VIDEO", patient_id="VID-1")
    ds.PixelData = encapsulate([stream])
    ds.Rows, ds.Columns, ds.NumberOfFrames = 64, 64, 10
    ds.file_meta.TransferSyntaxUID = "1.2.840.10008.1.2.4.102"
    f = _write(scp["tmp"], "video.dcm", ds)
    _storescu(scp, "CORPUS", [f], "-xn")
    inst = index.instance_paths(sop_uids=[ds.SOPInstanceUID])[0]
    assert inst["transfer_syntax"] == "1.2.840.10008.1.2.4.102"
    data, mime = render.video_stream(render.load(get_storage().get(inst["path"]), pixels=True))
    assert mime == "video/mp4" and data[4:8] == b"ftyp"
