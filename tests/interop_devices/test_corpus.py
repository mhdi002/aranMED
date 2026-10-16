"""Real-world DICOM files, sent by dcmtk ``storescu`` exactly as stored.

The corpus is pydicom's bundled test files plus, when available, the
pydicom-data collection (NEMA WG04 compression samples, Siemens MR with
overlays, ALOKA ultrasound, enhanced CT/MR, JPEG-LS/J2K/HTJ2K/RLE/deflate,
big endian, palette/YBR colour, parametric maps). Every object a device
could send must be stored; every image must render or be refused cleanly.
"""
from __future__ import annotations

from pathlib import Path

import pydicom
import pytest

from tests.functional import device_factory as F
from tests.interop_devices.conftest import CORPUS, SCP_AE, run

pytestmark = pytest.mark.interop

# Stored, but not displayable — each for a stated reason:
KNOWN_UNDISPLAYABLE = {
    # damaged on purpose in the corpora
    "emri_small_jpeg_2k_lossless_too_short", "JPEG-lossy", "JPEG2000-embedded-sequence-delimiter",
    "bad_sequence", "MR_truncated", "badVR",
    # 1-bit segmentation masks (SEG) in RLE / Deflated Image Frame: no 1-bit
    # decoder for these in pydicom 3.0 (the uncompressed variants display)
    "liver_rle", "liver_deflate", "liver_nonbyte_aligned", "liver_nonbyte_aligned_deflate",
    "liver_nonbyte_aligned_j2k", "liver_nonbyte_aligned_rle",
}


# Transfer syntaxes newer than dcmtk 3.6.7 (HTJ2K, Deflated Image Frame
# Compression, JPEG XL): those files are sent with pynetdicom instead.
DCMTK_367_UNSUPPORTED = {"1.2.840.10008.1.2.4.201", "1.2.840.10008.1.2.4.202",
                         "1.2.840.10008.1.2.4.203", "1.2.840.10008.1.2.4.204",
                         "1.2.840.10008.1.2.4.205", "1.2.840.10008.1.2.8.1",
                         "1.2.840.10008.1.2.4.110", "1.2.840.10008.1.2.4.111",
                         "1.2.840.10008.1.2.4.112"}
# Truncated on purpose: no sender can even read them.
UNSENDABLE = {"rtplan_truncated"}


def _pynetdicom_send(scp, aet: str, files: list[Path]) -> str:
    from pynetdicom import AE
    out = []
    for f in files:
        ds = pydicom.dcmread(f, force=True)
        ae = AE(ae_title=aet)
        ae.add_requested_context(ds.SOPClassUID, [str(ds.file_meta.TransferSyntaxUID)])
        assoc = ae.associate("127.0.0.1", scp["port"], ae_title=SCP_AE)
        if not assoc.is_established:
            out.append(f"{f.name}: association failed")
            continue
        st = assoc.send_c_store(ds)
        out.append(f"{f.name}: status {getattr(st, 'Status', None)}")
        assoc.release()
    return "\n".join(out)


def send_as_stored(scp, aet: str, files: list[Path], workdir: Path):
    """Send each file in its own transfer syntax, as a configured modality
    does: one dcmtk association profile per syntax, listing the SOP classes
    of the files in that syntax (dcmtk proposes uncompressed only otherwise)."""
    groups: dict[str, dict[str, list[Path]]] = {}
    newer: list[Path] = []
    for f in files:
        ds = pydicom.dcmread(f, stop_before_pixels=True, force=True)
        if str(ds.file_meta.TransferSyntaxUID) in DCMTK_367_UNSUPPORTED:
            newer.append(f)
            continue
        sop_class = ds.get("SOPClassUID") or ds.file_meta.get("MediaStorageSOPClassUID")
        groups.setdefault(str(ds.file_meta.TransferSyntaxUID), {}).setdefault(
            str(sop_class), []).append(f)
    outputs = []
    for i, (ts, by_class) in enumerate(sorted(groups.items())):
        classes = sorted(by_class)
        for chunk in range(0, len(classes), 120):
            part = classes[chunk:chunk + 120]
            cfg = workdir / f"profile{i}_{chunk}.cfg"
            lines = ["[[TransferSyntaxes]]", "[TS]", f"TransferSyntax1 = {ts}", "",
                     "[[PresentationContexts]]", "[PC]"]
            lines += [f"PresentationContext{n + 1} = {c}\\TS" for n, c in enumerate(part)]
            lines += ["", "[[Profiles]]", "[P]", "PresentationContexts = PC", ""]
            cfg.write_text("\n".join(lines))
            paths = [str(f) for c in part for f in by_class[c]]
            r = run(["storescu", "-aet", aet, "-aec", SCP_AE, "-xf", str(cfg), "P", "-nh",
                     "127.0.0.1", str(scp["port"]), *paths], timeout=600)
            outputs.append(r.stdout + r.stderr)
    if newer:
        outputs.append(_pynetdicom_send(scp, aet, newer))
    return "\n".join(outputs)


def _collect() -> list[Path]:
    files = sorted(F.SAMPLES.glob("*.dcm"))
    if CORPUS.is_dir():
        files += sorted(CORPUS.glob("*.dcm"))
    return files


@pytest.mark.device(vendor="Multi-vendor real-file corpus (pydicom + pydicom-data / NEMA WG04)",
                    service="C-STORE of every real object, any transfer syntax; display check")
def test_real_file_corpus(scp):
    from pacs import index, render
    from pacs.storage import get_storage
    out = scp["tmp"] / "corpus"
    out.mkdir()
    expected: dict[str, str] = {}
    for p in _collect():
        if p.stem in UNSENDABLE:
            continue
        try:
            ds = pydicom.dcmread(p, force=True)
            if "SOPClassUID" not in ds or "file_meta" not in dir(ds) or \
                    "TransferSyntaxUID" not in ds.file_meta:
                continue          # not a sendable composite object
            ds = F.fresh_uids(ds)
            ds.save_as(out / f"{p.stem}.dcm", enforce_file_format=True)
        except Exception:  # noqa: BLE001  (unwritable sample: not something a device sends)
            continue
        expected[str(ds.SOPInstanceUID)] = p.stem
    assert len(expected) > 60, f"corpus too small ({len(expected)} files)"

    log = send_as_stored(scp, "CORPUS", sorted(out.glob("*.dcm")), scp["tmp"])
    stored = {i["sop_uid"]: i for i in index.instance_paths(sop_uids=list(expected))}
    missing = sorted(expected[u] for u in expected if u not in stored)
    assert not missing, f"not stored: {missing}\n{log[-4000:]}"

    st = get_storage()
    undisplayable = []
    images = 0
    for uid, inst in stored.items():
        ds = render.load(st.get(inst["path"]), pixels=True)
        if "PixelData" not in ds and "FloatPixelData" not in ds and "DoubleFloatPixelData" not in ds:
            continue
        if render.is_video(ds):
            continue
        images += 1
        try:
            assert render.render(st.get(inst["path"]), frame=1)
        except Exception:  # noqa: BLE001
            undisplayable.append(expected[uid])
    unexpected = sorted(set(undisplayable) - KNOWN_UNDISPLAYABLE)
    assert images > 50
    assert not unexpected, f"images that failed to render: {unexpected}"
