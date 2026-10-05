"""DICOM objects as real devices send them, for the PACS device-support suite.

There are two sources:

* **Real-world files** shipped with pydicom (``pydicom/data/test_files``).
  They come from GE, Siemens, DCMTK, GDCM and dcm4che pipelines and cover:
  - 12-bit JPEG Extended, JPEG Lossless, JPEG-LS (lossless and
    near-lossless), JPEG 2000 and RLE (8/16/32-bit);
  - uncompressed YBR, palette colour and Big Endian;
  - Deflate, RT Dose / Plan / Struct, SR and ECG waveform.
* **Synthetic objects** for each modality family, built with known pixels
  and encoded into each transfer syntax with pydicom's own encoders, GDCM
  and Pillow, so a decoded pixel can be compared with the original
  exactly:
  - CT, MR, DX (MONOCHROME1), MG, PT, NM (palette colour);
  - US (RGB and multi-frame cine), XA multi-frame;
  - Enhanced MR (per-frame functional groups), Secondary Capture
    (planar configuration 1);
  - SEG (1-bit), RT Dose (32-bit), SR, Encapsulated PDF, video and a
    vendor-private class.
"""
from __future__ import annotations

import copy
import io
import os
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import (DeflatedExplicitVRLittleEndian, ExplicitVRBigEndian, ExplicitVRLittleEndian,
                         ImplicitVRLittleEndian, JPEG2000Lossless, JPEGBaseline8Bit, JPEGLosslessSV1,
                         JPEGLSLossless, RLELossless, generate_uid)

SAMPLES = Path(pydicom.__file__).parent / "data" / "test_files"
CHARSETS = Path(pydicom.__file__).parent / "data" / "charset_files"
PRIVATE_SOP = "1.2.840.113619.4.27"            # vendor-private raw-data class (GE)
MPEG4_AVC = "1.2.840.10008.1.2.4.102"           # MPEG-4 AVC/H.264 High Profile


# --------------------------------------------------------------------------- real-world files
def sample(name: str, *, charset_dir: bool = False) -> Dataset:
    ds = pydicom.dcmread(str((CHARSETS if charset_dir else SAMPLES) / name), force=True)
    if not getattr(ds, "file_meta", None) or "TransferSyntaxUID" not in ds.file_meta:
        fm = FileMetaDataset()
        fm.TransferSyntaxUID = ImplicitVRLittleEndian
        ds.file_meta = fm
    return ds


def fresh_uids(ds: Dataset, *, study_uid: Optional[str] = None, patient_id: Optional[str] = None,
               patient_name: Optional[str] = None) -> Dataset:
    """New study/series/instance UIDs (and optionally patient) so each sample
    is a distinct object; pixel encoding is left exactly as the device made it."""
    ds = copy.deepcopy(ds)
    ds.StudyInstanceUID = study_uid or generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    if "SOPClassUID" in ds:
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    if patient_id:
        ds.PatientID = patient_id
    if patient_name:
        ds.PatientName = patient_name
    return ds


# Real files: name -> what kind of decoding it exercises.
REAL_IMAGES = {
    "CT_small.dcm": "CT, Explicit VR LE, signed 16-bit",
    "MR_small_implicit.dcm": "MR, Implicit VR LE",
    "MR_small_bigendian.dcm": "MR, Explicit VR Big Endian",
    "image_dfl.dcm": "Deflated Explicit VR LE",
    "MR_small_RLE.dcm": "RLE Lossless 16-bit",
    "MR_small_jpeg_ls_lossless.dcm": "JPEG-LS Lossless",
    "JPEGLSNearLossless_16.dcm": "JPEG-LS near-lossless 16-bit",
    "MR_small_jp2klossless.dcm": "JPEG 2000 Lossless",
    "JPEG2000.dcm": "JPEG 2000 (lossy)",
    "693_J2KI.dcm": "JPEG 2000 irreversible",
    "JPGExtended.dcm": "JPEG Extended 12-bit (Process 2/4)",
    "SC_rgb_jpeg_gdcm.dcm": "JPEG Lossless SV1 RGB (GDCM)",
    "SC_rgb_jpeg_dcmtk.dcm": "JPEG Baseline YBR_FULL_422 (DCMTK)",
    "SC_rgb_jls_lossy_sample.dcm": "JPEG-LS near-lossless RGB",
    "SC_rgb_rle_32bit_2frame.dcm": "RLE 32-bit RGB, 2 frames",
    "SC_ybr_full_422_uncompressed.dcm": "uncompressed YBR_FULL_422",
    "examples_ybr_color.dcm": "YBR_FULL colour, multi-frame",
    "examples_palette.dcm": "PALETTE COLOR",
    "examples_jpeg2k.dcm": "JPEG 2000 RGB",
    "rtdose.dcm": "RT Dose, 32-bit multi-frame with grid scaling",
    "rtdose_rle.dcm": "RT Dose, RLE 32-bit",
    "liver_1frame.dcm": "Enhanced multi-frame (functional groups)",
}
# Corrupt streams from the field: stored and indexed, refused clearly on display.
REAL_MALFORMED = {
    "JPEG-lossy.dcm": "JPEG Extended with invalid SOS parameters (NEMA WG04 NM1_JPLY)",
    "JPEG2000-embedded-sequence-delimiter.dcm": "JPEG 2000 with a sequence delimiter inside the codestream",
}
REAL_NON_IMAGES = {
    "test-SR.dcm": "Structured Report",
    "rtplan.dcm": "RT Plan",
    "rtstruct.dcm": "RT Structure Set",
    "waveform_ecg.dcm": "12-lead ECG waveform",
}


# --------------------------------------------------------------------------- synthetic devices
def _header(sop_class: str, modality: str, *, study_uid: str, patient_name: str, patient_id: str,
            series_desc: str, charset: Optional[str] = "ISO_IR 192") -> Dataset:
    ds = Dataset()
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = generate_uid()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = generate_uid()
    if charset:
        ds.SpecificCharacterSet = charset
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.PatientBirthDate = "19700101"
    ds.PatientSex = "F"
    ds.StudyDate, ds.StudyTime, ds.StudyID = "20261001", "093000", "1"
    ds.AccessionNumber = "DEV-" + patient_id[-6:]
    ds.StudyDescription = f"{modality} DEVICE TEST"
    ds.SeriesDescription = series_desc
    ds.Modality = modality
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.ReferringPhysicianName = "Device^Test"
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = sop_class
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = fm
    return ds


def _mono(ds: Dataset, arr: np.ndarray, *, bits_stored: int, signed: bool = False,
          photometric: str = "MONOCHROME2") -> Dataset:
    ds.Rows, ds.Columns = arr.shape[-2:]
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = photometric
    ds.BitsAllocated = arr.dtype.itemsize * 8
    ds.BitsStored = bits_stored
    ds.HighBit = bits_stored - 1
    ds.PixelRepresentation = 1 if signed else 0
    if arr.ndim == 3:
        ds.NumberOfFrames = arr.shape[0]
    ds.PixelData = np.ascontiguousarray(arr).tobytes()
    return ds


def _rgb(ds: Dataset, arr: np.ndarray, *, planar: int = 0) -> Dataset:
    ds.Rows, ds.Columns = arr.shape[-3], arr.shape[-2]
    ds.SamplesPerPixel = 3
    ds.PhotometricInterpretation = "RGB"
    ds.PlanarConfiguration = planar
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 8, 8, 7, 0
    if arr.ndim == 4:
        ds.NumberOfFrames = arr.shape[0]
    data = arr
    if planar == 1:
        data = np.moveaxis(arr, -1, -3)  # (frames,) samples, rows, cols
    ds.PixelData = np.ascontiguousarray(data).tobytes()
    return ds


def _phantom(rows: int = 48, cols: int = 48, *, lo: int = 0, hi: int = 1000, frames: int = 1,
             dtype=np.uint16) -> np.ndarray:
    yy, xx = np.mgrid[0:rows, 0:cols]
    out = []
    for f in range(frames):
        ramp = lo + (xx + yy) * (hi - lo) // (rows + cols)
        disc = ((yy - rows // 2) ** 2 + (xx - cols // 2 - f) ** 2) < (rows // 4) ** 2
        out.append(np.where(disc, hi, ramp))
    arr = np.stack(out).astype(dtype)
    return arr if frames > 1 else arr[0]


def _rgb_phantom(rows: int = 48, cols: int = 48, frames: int = 1) -> np.ndarray:
    """Grey ramp with a pure red square at the centre (colour checks)."""
    out = []
    for f in range(frames):
        g = np.tile(np.linspace(30, 200, cols, dtype=np.uint8), (rows, 1))
        img = np.stack([g, g, g], axis=-1)
        r0, c0 = rows // 2 - 8, cols // 2 - 8 + f
        img[r0:r0 + 16, c0:c0 + 16] = (255, 0, 0)
        out.append(img)
    arr = np.stack(out)
    return arr if frames > 1 else arr[0]


def make_device(kind: str, *, study_uid: Optional[str] = None, patient_name: str = "Device^Patient",
                patient_id: str = "DEV-000001", charset: Optional[str] = "ISO_IR 192") -> Dataset:
    from pydicom import uid as U
    study_uid = study_uid or generate_uid()
    h = dict(study_uid=study_uid, patient_name=patient_name, patient_id=patient_id, charset=charset)
    if kind == "CT":
        ds = _header(U.CTImageStorage, "CT", series_desc="AXIAL", **h)
        ds = _mono(ds, (_phantom(lo=-1000, hi=900, dtype=np.int16)), bits_stored=16, signed=True)
        ds.RescaleIntercept, ds.RescaleSlope, ds.WindowCenter, ds.WindowWidth = 0, 1, 40, 400
    elif kind == "MR":
        ds = _header(U.MRImageStorage, "MR", series_desc="T2", **h)
        ds = _mono(ds, _phantom(hi=3000), bits_stored=12)
    elif kind == "DX":
        ds = _header(U.DigitalXRayImageStorageForPresentation, "DX", series_desc="PA", **h)
        ds = _mono(ds, _phantom(hi=16000), bits_stored=14, photometric="MONOCHROME1")
        ds.PresentationIntentType = "FOR PRESENTATION"
    elif kind == "MG":
        ds = _header(U.DigitalMammographyXRayImageStorageForPresentation, "MG", series_desc="CC", **h)
        ds = _mono(ds, _phantom(hi=4000), bits_stored=12)
    elif kind == "PT":
        ds = _header(U.PositronEmissionTomographyImageStorage, "PT", series_desc="WB", **h)
        ds = _mono(ds, _phantom(hi=20000), bits_stored=16)
        ds.RescaleSlope, ds.RescaleIntercept, ds.Units = 0.25, 0, "BQML"
    elif kind == "NM":
        ds = _header(U.NuclearMedicineImageStorage, "NM", series_desc="BONE", **h)
        idx = _phantom(hi=255, dtype=np.uint8)
        ds = _mono(ds, idx, bits_stored=8, photometric="PALETTE COLOR")
        ramp = np.arange(256, dtype=np.uint16) * 257          # 16-bit LUT entries
        for color, vals in (("Red", ramp), ("Green", ramp // 2), ("Blue", np.zeros(256, np.uint16))):
            setattr(ds, f"{color}PaletteColorLookupTableDescriptor", [256, 0, 16])
            setattr(ds, f"{color}PaletteColorLookupTableData", vals.astype("<u2").tobytes())
    elif kind == "US":
        ds = _header(U.UltrasoundImageStorage, "US", series_desc="ABDOMEN", **h)
        ds = _rgb(ds, _rgb_phantom())
    elif kind == "US_CINE":
        ds = _header(U.UltrasoundMultiFrameImageStorage, "US", series_desc="CINE", **h)
        ds = _rgb(ds, _rgb_phantom(frames=4))
        ds.FrameTime = 33.3
    elif kind == "XA":
        ds = _header(U.XRayAngiographicImageStorage, "XA", series_desc="LCA RAO", **h)
        ds = _mono(ds, _phantom(hi=255, frames=6, dtype=np.uint8), bits_stored=8)
        ds.FrameTime = 66.7
    elif kind == "EMR":
        ds = _header(U.EnhancedMRImageStorage, "MR", series_desc="ENHANCED", **h)
        ds = _mono(ds, _phantom(hi=2000, frames=3), bits_stored=12)
        shared = Dataset()
        voi = Dataset()
        voi.WindowCenter, voi.WindowWidth = 1000, 2000
        shared.FrameVOILUTSequence = Sequence([voi])
        ds.SharedFunctionalGroupsSequence = Sequence([shared])
        per = []
        for f in range(3):
            item, pvt = Dataset(), Dataset()
            pvt.RescaleSlope, pvt.RescaleIntercept, pvt.RescaleType = f + 1, 10 * f, "US"
            item.PixelValueTransformationSequence = Sequence([pvt])
            per.append(item)
        ds.PerFrameFunctionalGroupsSequence = Sequence(per)
    elif kind == "SC":
        ds = _header(U.SecondaryCaptureImageStorage, "OT", series_desc="SCREENSHOT", **h)
        ds = _rgb(ds, _rgb_phantom(), planar=1)
    elif kind == "SEG":
        ds = _header(U.SegmentationStorage, "SEG", series_desc="LIVER MASK", **h)
        mask = (_phantom(hi=1, frames=2, dtype=np.uint8) > 0).astype(np.uint8)
        ds.Rows, ds.Columns = mask.shape[-2:]
        ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
        ds.BitsAllocated = ds.BitsStored = 1
        ds.HighBit, ds.PixelRepresentation, ds.NumberOfFrames = 0, 0, 2
        ds.SegmentationType = "BINARY"
        from pydicom.pixels import pack_bits
        ds.PixelData = pack_bits(mask)
    elif kind == "RTDOSE":
        ds = _header(U.RTDoseStorage, "RTDOSE", series_desc="DOSE", **h)
        dose = _phantom(hi=200000, frames=2, dtype=np.uint32)
        ds = _mono(ds, dose, bits_stored=32)
        ds.DoseGridScaling, ds.DoseUnits, ds.DoseType = 0.0001, "GY", "PHYSICAL"
        ds.GridFrameOffsetVector = [0, 3]
    elif kind == "SR":
        ds = _header(U.BasicTextSRStorage, "SR", series_desc="REPORT", **h)
        ds.ValueType, ds.ContinuityOfContent = "CONTAINER", "SEPARATE"
        title = Dataset()
        title.CodeValue, title.CodingSchemeDesignator, title.CodeMeaning = "18748-4", "LN", "Diagnostic Imaging Report"
        ds.ConceptNameCodeSequence = Sequence([title])
        ds.CompletionFlag, ds.VerificationFlag = "COMPLETE", "VERIFIED"
        items = []
        for name, text in (("Findings", "Consolidation in the right lower lobe."),
                           ("Impression", "Pneumonia. یافته‌ها با پنومونی سازگار است.")):
            it, cn = Dataset(), Dataset()
            it.RelationshipType, it.ValueType = "CONTAINS", "TEXT"
            cn.CodeValue, cn.CodingSchemeDesignator, cn.CodeMeaning = name[:8].upper(), "99LOCAL", name
            it.ConceptNameCodeSequence = Sequence([cn])
            it.TextValue = text
            items.append(it)
        ds.ContentSequence = Sequence(items)
    elif kind == "PDF":
        ds = _header(U.EncapsulatedPDFStorage, "DOC", series_desc="REFERRAL", **h)
        pdf = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>"
               b"endobj\n3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
        ds.MIMETypeOfEncapsulatedDocument = "application/pdf"
        ds.DocumentTitle = "Referral letter"
        ds.BurnedInAnnotation = "YES"
        ds.EncapsulatedDocument = pdf + (b"\x00" if len(pdf) % 2 else b"")
        ds.EncapsulatedDocumentLength = len(pdf)
    elif kind == "VIDEO":
        ds = _header(U.VideoEndoscopicImageStorage, "ES", series_desc="COLONOSCOPY", **h)
        stream = b"\x00\x00\x00\x01\x67\x64\x00\x1f" + bytes(range(256)) * 4   # H.264 NAL units
        ds.Rows, ds.Columns, ds.SamplesPerPixel = 480, 640, 3
        ds.PhotometricInterpretation, ds.PlanarConfiguration = "YBR_PARTIAL_420", 0
        ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 8, 8, 7, 0
        ds.NumberOfFrames, ds.FrameTime = 30, 33.3
        from pydicom.encaps import encapsulate
        ds.PixelData = encapsulate([stream])
        ds["PixelData"].VR = "OB"
        ds.file_meta.TransferSyntaxUID = MPEG4_AVC
    elif kind == "PRIVATE":
        ds = _header(PRIVATE_SOP, "OT", series_desc="RAW DATA", **h)
        ds.add_new(0x00431010, "LO", "vendor raw payload")
    else:
        raise ValueError(kind)
    return ds


# Lossless syntaxes give back exactly the original pixels; JPEG baseline is lossy.
LOSSLESS = ("explicit", "implicit", "bigendian", "deflate", "rle", "jpegls", "j2k", "jpeg_lossless")
SYNTAX_UID = {"explicit": ExplicitVRLittleEndian, "implicit": ImplicitVRLittleEndian,
              "bigendian": ExplicitVRBigEndian, "deflate": DeflatedExplicitVRLittleEndian,
              "rle": RLELossless, "jpegls": JPEGLSLossless, "j2k": JPEG2000Lossless,
              "jpeg_lossless": JPEGLosslessSV1, "jpeg_baseline": JPEGBaseline8Bit}


def original_pixels(ds: Dataset) -> np.ndarray:
    """Ground truth from pydicom itself (uncompressed source)."""
    from pydicom.pixels import pixel_array
    return pixel_array(ds)


def encode(ds: Dataset, syntax: str) -> Dataset:
    """A copy of uncompressed *ds* in transfer syntax *syntax* (key of SYNTAX_UID)."""
    out = copy.deepcopy(ds)
    out.SOPInstanceUID = generate_uid()
    out.file_meta.MediaStorageSOPInstanceUID = out.SOPInstanceUID
    uid = SYNTAX_UID[syntax]
    if syntax in ("explicit", "implicit", "deflate"):
        out.file_meta.TransferSyntaxUID = uid
        return out
    if syntax == "bigendian":
        if int(out.BitsAllocated) > 8:
            arr = np.frombuffer(out.PixelData, dtype=f"<u{int(out.BitsAllocated) // 8}")
            out.PixelData = arr.byteswap().tobytes()
        out.file_meta.TransferSyntaxUID = uid
        return out
    if getattr(out, "PlanarConfiguration", 0) == 1:
        from pydicom.pixels import pixel_array
        arr = pixel_array(out)
        out.PlanarConfiguration = 0
        out.PixelData = np.ascontiguousarray(arr).tobytes()
    if syntax in ("rle", "jpegls", "j2k"):
        out.compress(uid)
        return out
    if syntax == "jpeg_lossless":
        return _gdcm(out, "JPEGLosslessProcess14_1")
    if syntax == "jpeg_baseline":
        return _pillow_jpeg(out)
    raise ValueError(syntax)


def _gdcm(ds: Dataset, ts_name: str) -> Dataset:
    import gdcm
    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds, enforce_file_format=True)
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in.dcm"), os.path.join(d, "out.dcm")
        open(src, "wb").write(buf.getvalue())
        r = gdcm.ImageReader()
        r.SetFileName(src)
        assert r.Read()
        ch = gdcm.ImageChangeTransferSyntax()
        ch.SetTransferSyntax(gdcm.TransferSyntax(getattr(gdcm.TransferSyntax, ts_name)))
        ch.SetInput(r.GetImage())
        assert ch.Change()
        w = gdcm.ImageWriter()
        w.SetFileName(dst)
        w.SetFile(r.GetFile())
        w.SetImage(ch.GetOutput())
        assert w.Write()
        return pydicom.dcmread(dst)


def _pillow_jpeg(ds: Dataset) -> Dataset:
    from PIL import Image
    from pydicom.encaps import encapsulate
    from pydicom.pixels import pixel_array
    arr = pixel_array(ds)
    frames = arr if int(getattr(ds, "NumberOfFrames", 1) or 1) > 1 else arr[None]
    color = int(ds.SamplesPerPixel) == 3
    blobs = []
    for f in frames:
        img = Image.fromarray(f.astype(np.uint8), mode="RGB" if color else "L")
        b = io.BytesIO()
        img.save(b, format="JPEG", quality=95, subsampling=0 if color else None) if color \
            else img.save(b, format="JPEG", quality=95)
        blobs.append(b.getvalue())
    ds.PixelData = encapsulate(blobs)
    ds["PixelData"].VR = "OB"
    if color:
        ds.PhotometricInterpretation = "YBR_FULL"   # Pillow stores YCbCr
    ds.file_meta.TransferSyntaxUID = JPEGBaseline8Bit
    return ds


def to_bytes(ds: Dataset) -> bytes:
    buf = io.BytesIO()
    big = str(ds.file_meta.TransferSyntaxUID) == ExplicitVRBigEndian
    if big:
        ds.preamble = b"\x00" * 128
        pydicom.dcmwrite(buf, ds, little_endian=False, implicit_vr=False, enforce_file_format=True)
    else:
        pydicom.dcmwrite(buf, ds, enforce_file_format=True)
    return buf.getvalue()


# Which syntaxes each synthetic device is sent in (as such devices do).
DEVICE_SYNTAXES = {
    "CT": ("explicit", "implicit", "bigendian", "deflate", "rle", "jpegls", "j2k", "jpeg_lossless"),
    "MR": ("explicit", "implicit", "bigendian", "rle", "jpegls", "j2k", "jpeg_lossless"),
    "DX": ("explicit", "rle", "jpegls", "j2k", "jpeg_lossless"),
    "MG": ("explicit", "jpegls", "j2k", "jpeg_lossless"),
    "PT": ("explicit", "implicit", "rle", "j2k"),
    "NM": ("explicit", "rle", "jpegls"),
    "US": ("explicit", "rle", "jpegls", "j2k", "jpeg_baseline"),
    "US_CINE": ("explicit", "rle", "jpeg_baseline"),
    "XA": ("explicit", "rle", "jpegls", "jpeg_baseline"),
    "EMR": ("explicit", "rle", "j2k"),
    "SC": ("explicit", "bigendian", "rle", "jpeg_baseline"),
    "SEG": ("explicit", "implicit"),
    "RTDOSE": ("explicit", "bigendian"),
}
