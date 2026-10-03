"""Common interface every remote-PACS adapter implements.

The federation layer and the retrieve/send jobs only ever talk to this
interface, so a hospital's existing archive (dcm4chee, Orthanc, a vendor
PACS on DIMSE, another AranMed) plugs in by node configuration alone.
"""
from __future__ import annotations

import abc
from typing import Any, Optional


class AdapterError(RuntimeError):
    pass


class RemotePacs(abc.ABC):
    def __init__(self, node: dict) -> None:
        self.node = node

    @property
    def label(self) -> str:
        return self.node.get("name") or self.node.get("ae_title") or "remote"

    @abc.abstractmethod
    def echo(self) -> bool: ...

    @abc.abstractmethod
    def search_studies(self, filters: dict[str, Any], limit: int = 100) -> list[dict]:
        """Keyword dicts shaped like :func:`pacs.index.query_studies` output."""

    @abc.abstractmethod
    def list_series(self, study_uid: str) -> list[dict]: ...

    @abc.abstractmethod
    def retrieve_study(self, study_uid: str, series_uid: Optional[str] = None) -> dict:
        """Fetch into the local archive. Returns ``{stored, failed}``."""

    @abc.abstractmethod
    def send_study(self, study_uid: str) -> dict:
        """Push a locally stored study to the remote. Returns ``{sent, failed}``."""

    def tag(self, item: dict) -> dict:
        ext = dict(item.get("_ext") or {})
        ext.update({"remote_node": self.node.get("id"), "remote_name": self.label,
                    "remote_kind": self.node.get("kind"),
                    "origin_facility": ext.get("origin_facility") or self.node.get("facility_oid")})
        return {**item, "_ext": ext}


def keyword_dict_from_json(js: dict) -> dict[str, Any]:
    """DICOM JSON object -> {Keyword: python value} (strings / lists)."""
    from pydicom.dataset import Dataset
    ds = Dataset.from_json(js)
    out: dict[str, Any] = {}
    for elem in ds:
        if not elem.keyword or elem.VR == "SQ":
            continue
        v = elem.value
        if v.__class__.__name__ == "MultiValue":
            v = [str(x) for x in v]
        elif v is not None and not isinstance(v, (int, float)):
            v = str(v)
        out[elem.keyword] = v
    if isinstance(out.get("ModalitiesInStudy"), str):
        out["ModalitiesInStudy"] = [m for m in out["ModalitiesInStudy"].split("\\") if m]
    return out
