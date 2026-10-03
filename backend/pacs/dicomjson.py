"""Conversions between index rows, pydicom Datasets and the DICOM JSON model.

DICOM JSON (PS3.18 Annex F) is what QIDO-RS and WADO-RS metadata speak;
pydicom Datasets are what C-FIND answers with. Both are produced from the
same keyword-keyed dicts the index query engine returns, so the two
protocols can never disagree about what a study contains.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydicom.dataset import Dataset

# Attributes returned at each level by default (PS3.18 Table 6.7.1-2 ff.).
STUDY_RETURN = ["SpecificCharacterSet", "StudyDate", "StudyTime", "AccessionNumber",
                "ModalitiesInStudy", "ReferringPhysicianName", "PatientName", "PatientID",
                "IssuerOfPatientID", "PatientBirthDate", "PatientSex", "StudyInstanceUID",
                "StudyID", "StudyDescription", "NumberOfStudyRelatedSeries",
                "NumberOfStudyRelatedInstances"]
SERIES_RETURN = ["Modality", "SeriesInstanceUID", "SeriesNumber", "SeriesDescription",
                 "BodyPartExamined", "Laterality", "NumberOfSeriesRelatedInstances",
                 "StudyInstanceUID"]
INSTANCE_RETURN = ["SOPClassUID", "SOPInstanceUID", "InstanceNumber", "Rows", "Columns",
                   "NumberOfFrames", "SeriesInstanceUID", "StudyInstanceUID"]
PATIENT_RETURN = ["PatientName", "PatientID", "IssuerOfPatientID", "PatientBirthDate",
                  "PatientSex", "NumberOfPatientRelatedStudies"]


def to_dataset(item: dict[str, Any], keys: Optional[list[str]] = None) -> Dataset:
    ds = Dataset()
    ds.SpecificCharacterSet = "ISO_IR 192"  # UTF-8: Persian names survive the wire
    for k in keys or [k for k in item if not k.startswith("_")]:
        if k == "SpecificCharacterSet" or k.startswith("_"):
            continue
        v = item.get(k)
        if v is None or v == []:
            # Return keys requested but unknown as empty (universal matching).
            try:
                setattr(ds, k, "" if k not in ("NumberOfFrames", "Rows", "Columns",
                                               "InstanceNumber", "SeriesNumber") else None)
            except Exception:  # noqa: BLE001
                pass
            continue
        try:
            setattr(ds, k, v)
        except Exception:  # noqa: BLE001
            setattr(ds, k, str(v))
    return ds


def to_json(item: dict[str, Any], keys: Optional[list[str]] = None,
            retrieve_url: Optional[str] = None) -> dict:
    ds = to_dataset(item, keys)
    out = ds.to_json_dict()
    if retrieve_url:
        out["00081190"] = {"vr": "UR", "Value": [retrieve_url]}
    return out


def dataset_metadata_json(ds: Dataset, bulk_uri: Callable[[str], str]) -> dict:
    """Full instance metadata with pixel data replaced by a BulkDataURI."""
    def handler(elem) -> str:
        return bulk_uri(f"{elem.tag:08X}")
    return ds.to_json_dict(bulk_data_threshold=1024, bulk_data_element_handler=handler)


def requested_keys(default: list[str], includefield: Optional[list[str]]) -> list[str]:
    keys = list(default)
    for inc in includefield or []:
        for part in inc.split(","):
            part = part.strip()
            if not part or part == "all":
                continue
            if len(part) == 8 and all(ch in "0123456789abcdefABCDEF" for ch in part):
                from pydicom.datadict import keyword_for_tag
                part = keyword_for_tag(int(part, 16)) or part
            if part not in keys:
                keys.append(part)
    return keys
