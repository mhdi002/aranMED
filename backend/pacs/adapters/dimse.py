"""Classic DICOM networking adapter for vendor PACS / VNAs.

Retrieval uses C-MOVE to our own AE when the local SCP is running (the
remote must know our AE title/host/port — the usual PACS setup), otherwise
C-GET on the same association, which needs no inbound connectivity.
"""
from __future__ import annotations

from typing import Any, Optional

from pacs import dimse, scu
from pacs.adapters.base import AdapterError, RemotePacs

_RETURN = ["StudyInstanceUID", "StudyDate", "StudyTime", "AccessionNumber", "PatientName",
           "PatientID", "PatientBirthDate", "PatientSex", "StudyDescription",
           "ModalitiesInStudy", "ReferringPhysicianName", "NumberOfStudyRelatedSeries",
           "NumberOfStudyRelatedInstances"]


class DimseAdapter(RemotePacs):
    def echo(self) -> bool:
        return scu.echo(self.node)

    def search_studies(self, filters: dict[str, Any], limit: int = 100) -> list[dict]:
        allowed = set(_RETURN) | {"IssuerOfPatientID"}
        q = {k: v for k, v in filters.items() if k in allowed and v}
        try:
            rows = scu.find(self.node, "STUDY", q, [k for k in _RETURN if k not in q])
        except scu.ScuError as e:
            raise AdapterError(str(e)) from e
        out = []
        for r in rows[:limit]:
            mods = r.get("ModalitiesInStudy")
            if isinstance(mods, str):
                r["ModalitiesInStudy"] = [m for m in mods.split("\\") if m]
            out.append(self.tag(r))
        return out

    def list_series(self, study_uid: str) -> list[dict]:
        try:
            return scu.find(self.node, "SERIES", {"StudyInstanceUID": study_uid},
                            ["SeriesInstanceUID", "Modality", "SeriesNumber",
                             "SeriesDescription", "NumberOfSeriesRelatedInstances"])
        except scu.ScuError as e:
            raise AdapterError(str(e)) from e

    def retrieve_study(self, study_uid: str, series_uid: Optional[str] = None) -> dict:
        try:
            if dimse.status()["running"] and not self.node.get("prefer_cget"):
                r = scu.move(self.node, study_uid=study_uid, series_uid=series_uid)
                return {"stored": r["completed"], "failed": r["failed"]}
            r = scu.get(self.node, study_uid=study_uid, series_uid=series_uid)
            return {"stored": r["stored"], "failed": 0}
        except scu.ScuError as e:
            raise AdapterError(str(e)) from e

    def send_study(self, study_uid: str) -> dict:
        try:
            return scu.store_study(self.node, study_uid)
        except scu.ScuError as e:
            raise AdapterError(str(e)) from e
