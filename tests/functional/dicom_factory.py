"""Synthetic but fully valid DICOM objects with real pixel data.

Each image has structure (a bright disc on a ramp) so rendering, windowing
and frame extraction can be checked for real content, not just "bytes came
back".
"""
from __future__ import annotations

import io
from typing import Optional

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (CTImageStorage, ExplicitVRLittleEndian, MRImageStorage,
                         ComputedRadiographyImageStorage, generate_uid)

_CLASS = {"CT": CTImageStorage, "MR": MRImageStorage, "CR": ComputedRadiographyImageStorage}


def _pixels(rows: int, cols: int, seed: int, frames: int = 1) -> np.ndarray:
    yy, xx = np.mgrid[0:rows, 0:cols]
    out = []
    for f in range(frames):
        ramp = (xx * 4 + yy * 2).astype(np.int32)
        cy, cx = rows // 2 + (seed % 7) - 3 + f, cols // 2
        disc = ((yy - cy) ** 2 + (xx - cx) ** 2) < (min(rows, cols) // 4) ** 2
        img = ramp + disc * 1500 + seed * 3
        out.append(np.clip(img, 0, 4095).astype(np.uint16))
    return np.stack(out) if frames > 1 else out[0]


def make_instance(*, study_uid: str, series_uid: str, modality: str = "CT",
                  instance_number: int = 1, patient_name: str = "Test^Patient",
                  patient_id: str = "PID-1", birth_date: str = "19800101", sex: str = "M",
                  accession: str = "ACC-1", study_date: str = "20260901",
                  study_desc: str = "CT CHEST", series_desc: str = "AXIAL",
                  series_number: int = 1, body_part: str = "CHEST",
                  issuer: Optional[str] = None, rows: int = 64, cols: int = 64,
                  frames: int = 1, sop_uid: Optional[str] = None) -> Dataset:
    ds = Dataset()
    sop_class = _CLASS[modality]
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = sop_uid or generate_uid()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    if issuer:
        ds.IssuerOfPatientID = issuer
    ds.PatientBirthDate = birth_date
    ds.PatientSex = sex
    ds.AccessionNumber = accession
    ds.StudyDate = study_date
    ds.StudyTime = "101500"
    ds.StudyID = "1"
    ds.StudyDescription = study_desc
    ds.ReferringPhysicianName = "Ref^Doctor"
    ds.Modality = modality
    ds.SeriesNumber = series_number
    ds.SeriesDescription = series_desc
    ds.BodyPartExamined = body_part
    ds.InstanceNumber = instance_number
    ds.ImagePositionPatient = [0, 0, float(instance_number) * 5]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.PixelSpacing = [0.7, 0.7]
    ds.SliceThickness = 5
    ds.Rows, ds.Columns = rows, cols
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 12, 11
    ds.PixelRepresentation = 0
    if modality == "CT":
        ds.RescaleIntercept, ds.RescaleSlope = -1024, 1
        ds.WindowCenter, ds.WindowWidth = 40, 400
    else:
        ds.WindowCenter, ds.WindowWidth = 1200, 2400
    arr = _pixels(rows, cols, instance_number, frames)
    if frames > 1:
        ds.NumberOfFrames = frames
    ds.PixelData = arr.tobytes()
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = sop_class
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = fm
    return ds


def to_bytes(ds: Dataset) -> bytes:
    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds, enforce_file_format=True)
    return buf.getvalue()


def make_study(*, n_series: int = 3, per_series: int = 4, modality: str = "CT",
               study_uid: Optional[str] = None, **kw) -> tuple[str, list[Dataset]]:
    study_uid = study_uid or generate_uid()
    out = []
    for s in range(n_series):
        series_uid = generate_uid()
        for i in range(per_series):
            out.append(make_instance(study_uid=study_uid, series_uid=series_uid,
                                     modality=modality, series_number=s + 1,
                                     instance_number=i + 1,
                                     series_desc=f"SERIES {s + 1}", **kw))
    return study_uid, out
