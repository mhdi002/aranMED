"""DICOM ingestion — study metadata and rendered images from a DICOM file.

Radiology arrives as DICOM, not as a .m4a and a .png. This turns a Part 10
file into the two things the rest of the pipeline can already use:

* **Study context** — modality, body part, study/series/SOP UIDs, accession
  number, patient identifiers. That context is what makes a report
  attributable to a study, and it also drives template selection far more
  reliably than dictated words do: the modality tag says "US" with certainty,
  where the transcript says "sonography" and hopes.
* **A rendered image** the vision model can read, with the DICOM windowing
  (window centre/width, or a MONOCHROME1 inversion) applied — a raw 16-bit
  pixel array shown without windowing is a black rectangle.

Deliberately *not* here:

* No de-identification claim. Tags are extracted, not scrubbed; a DICOM file
  carries patient identifiers and this treats them as PHI, which means the
  extracted record goes through the same encrypted store as everything else.
* No SR/structured-report generation, no burnt-in annotation detection.
* Multi-frame files render their representative frame only.

Depends on ``pydicom`` (and ``numpy``/``Pillow``, already present).
"""
from __future__ import annotations

import io
import logging
from typing import Any, Optional

log = logging.getLogger("dicom")

# Tags worth carrying forward. Keeping this explicit rather than dumping every
# element means the payload stays small, predictable, and free of the private
# vendor tags that make DICOM dumps unreadable.
_META_TAGS: list[tuple[str, str]] = [
    ("SOPClassUID", "sop_class_uid"),
    ("SOPInstanceUID", "sop_instance_uid"),
    ("StudyInstanceUID", "study_instance_uid"),
    ("SeriesInstanceUID", "series_instance_uid"),
    ("AccessionNumber", "accession_number"),
    ("StudyID", "study_id"),
    ("StudyDate", "study_date"),
    ("StudyTime", "study_time"),
    ("StudyDescription", "study_description"),
    ("SeriesDescription", "series_description"),
    ("Modality", "modality"),
    ("BodyPartExamined", "body_part"),
    ("Laterality", "laterality"),
    ("ViewPosition", "view_position"),
    ("Manufacturer", "manufacturer"),
    ("ManufacturerModelName", "device_model"),
    ("InstitutionName", "institution"),
    ("ReferringPhysicianName", "referring_physician"),
    ("PatientID", "patient_mrn"),
    ("PatientName", "patient_name"),
    ("PatientBirthDate", "patient_birth_date"),
    ("PatientSex", "patient_sex"),
    ("PatientAge", "patient_age"),
]

# DICOM modality -> the words a report template is likely to be titled with.
# Used only as a hint alongside the transcript; the modality tag is a fact,
# the mapping to a template name is not.
MODALITY_HINTS: dict[str, str] = {
    "US": "sonography ultrasound doppler",
    "CT": "ct computed tomography",
    "MR": "mri magnetic resonance",
    "CR": "x-ray radiograph plain film",
    "DX": "x-ray radiograph digital",
    "MG": "mammography breast",
    "NM": "nuclear medicine scintigraphy",
    "PT": "pet positron emission",
    "XA": "angiography fluoroscopy",
    "RF": "fluoroscopy",
    "OT": "other",
}


def available() -> bool:
    try:
        import pydicom  # noqa: F401,PLC0415
        return True
    except ImportError:
        return False


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def read_metadata(data: bytes) -> dict[str, Any]:
    """Parse a DICOM file's headers. Does not decode pixels."""
    import pydicom  # noqa: PLC0415

    ds = pydicom.dcmread(io.BytesIO(data), stop_before_pixels=True, force=True)
    meta: dict[str, Any] = {}
    for tag, key in _META_TAGS:
        if tag in ds:
            meta[key] = _clean(getattr(ds, tag, None))
    meta = {k: v for k, v in meta.items() if v is not None}

    modality = (meta.get("modality") or "").upper()
    if modality:
        meta["modality_hint"] = MODALITY_HINTS.get(modality, modality.lower())
    # Headers are read with stop_before_pixels=True, so PixelData is by
    # definition absent from `ds` here — testing for it would report "no
    # image" for every image. Rows/Columns are what actually say this is a
    # pixel-bearing object, and they are in the header.
    meta["has_pixel_data"] = bool(getattr(ds, "Rows", 0)) and bool(getattr(ds, "Columns", 0))
    meta["frames"] = int(getattr(ds, "NumberOfFrames", 1) or 1)
    meta["rows"] = int(getattr(ds, "Rows", 0) or 0)
    meta["columns"] = int(getattr(ds, "Columns", 0) or 0)
    return meta


def render_png(data: bytes, *, frame: Optional[int] = None,
               max_size: int = 2048) -> bytes:
    """Render a frame to PNG with DICOM windowing applied.

    Without windowing a 12/16-bit modality image is effectively black, so a
    vision model reading the raw array would be describing nothing. This
    applies WindowCenter/WindowWidth when present, falls back to a min/max
    stretch when absent, and honours MONOCHROME1 (where high values are dark).
    """
    import numpy as np  # noqa: PLC0415
    import pydicom  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    ds = pydicom.dcmread(io.BytesIO(data), force=True)
    if "PixelData" not in ds:
        raise ValueError("DICOM file has no pixel data")

    arr = ds.pixel_array
    if arr.ndim == 4 or (arr.ndim == 3 and arr.shape[-1] not in (3, 4)):
        idx = frame if frame is not None else arr.shape[0] // 2
        arr = arr[max(0, min(idx, arr.shape[0] - 1))]

    if arr.ndim == 2:
        arr = arr.astype(np.float32)
        # Rescale to stored units first — slope/intercept are how CT reaches
        # Hounsfield units, and windowing is defined in those units.
        slope = float(getattr(ds, "RescaleSlope", 1) or 1)
        intercept = float(getattr(ds, "RescaleIntercept", 0) or 0)
        arr = arr * slope + intercept

        centre = getattr(ds, "WindowCenter", None)
        width = getattr(ds, "WindowWidth", None)
        if isinstance(centre, pydicom.multival.MultiValue):
            centre = centre[0]
        if isinstance(width, pydicom.multival.MultiValue):
            width = width[0]

        if centre is not None and width is not None and float(width) > 0:
            centre, width = float(centre), float(width)
            lo, hi = centre - width / 2.0, centre + width / 2.0
        else:
            lo, hi = float(arr.min()), float(arr.max())
        if hi <= lo:
            hi = lo + 1.0
        arr = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)

        if str(getattr(ds, "PhotometricInterpretation", "")).upper() == "MONOCHROME1":
            arr = 1.0 - arr
        arr = (arr * 255.0).astype(np.uint8)
        img = Image.fromarray(arr, mode="L")
    else:
        img = Image.fromarray(arr.astype(np.uint8))

    if max(img.size) > max_size:
        img.thumbnail((max_size, max_size))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def to_ehr_patient(meta: dict[str, Any]) -> dict[str, Any]:
    """The patient fields a DICOM header can populate, in the internal shape.

    DICOM ``PatientName`` uses ``Family^Given^Middle^Prefix^Suffix``; it is
    normalised to something a person would write.
    """
    name = meta.get("patient_name")
    if name and "^" in name:
        parts = [p for p in name.split("^") if p]
        if len(parts) >= 2:
            name = f"{parts[1]} {parts[0]}"
        elif parts:
            name = parts[0]

    age = meta.get("patient_age")
    if age and len(age) == 4 and age[-1].upper() == "Y":
        try:
            age = float(age[:3])
        except ValueError:
            age = None
    else:
        age = None

    sex_map = {"M": "male", "F": "female", "O": "other"}
    out: dict[str, Any] = {
        "name": name,
        "mrn": meta.get("patient_mrn"),
        "sex": sex_map.get((meta.get("patient_sex") or "").upper()),
        "age": age,
    }
    return {k: v for k, v in out.items() if v is not None}
