"""Orthanc native REST adapter (for Orthanc servers without the DICOMweb
plugin, or where its REST API is the integration point a hospital exposes).

Endpoints used: ``GET /system``, ``POST /tools/find``, ``POST /tools/lookup``,
``GET /studies/{id}/series``, ``GET /studies/{id}/instances``,
``GET /instances/{id}/file``, ``POST /instances``.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from clinicaldb import messages
from pacs import config, index, nodes
from pacs.adapters.base import AdapterError, RemotePacs


def _from_tags(main: dict, patient: dict, oid: str) -> dict[str, Any]:
    mods = main.get("ModalitiesInStudy") or ""
    return {
        "StudyInstanceUID": main.get("StudyInstanceUID"),
        "StudyDate": main.get("StudyDate"), "StudyTime": main.get("StudyTime"),
        "StudyDescription": main.get("StudyDescription"),
        "AccessionNumber": main.get("AccessionNumber"),
        "ReferringPhysicianName": main.get("ReferringPhysicianName"),
        "StudyID": main.get("StudyID"),
        "PatientName": patient.get("PatientName"), "PatientID": patient.get("PatientID"),
        "PatientBirthDate": patient.get("PatientBirthDate"),
        "PatientSex": patient.get("PatientSex"),
        "ModalitiesInStudy": [m for m in mods.split("\\") if m],
        "_ext": {"orthanc_id": oid},
    }


class OrthancAdapter(RemotePacs):
    @property
    def base(self) -> str:
        return (self.node.get("base_url") or "").rstrip("/")

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base, timeout=config.http_timeout(),
                            headers=nodes.auth_header(self.node))

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            with self._client() as c:
                r = c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise AdapterError(f"{self.label}: {e}") from e
        if r.status_code >= 400:
            raise AdapterError(f"{self.label}: {method} {path} -> HTTP {r.status_code}")
        return r

    def echo(self) -> bool:
        try:
            return self._call("GET", "/system").status_code == 200
        except AdapterError:
            return False

    def search_studies(self, filters: dict[str, Any], limit: int = 100) -> list[dict]:
        allowed = ("PatientName", "PatientID", "AccessionNumber", "StudyDate",
                   "StudyInstanceUID", "StudyDescription", "ModalitiesInStudy",
                   "PatientBirthDate", "ReferringPhysicianName")
        query = {k: str(v) for k, v in filters.items() if k in allowed and v}
        body = {"Level": "Study", "Query": query, "Expand": True, "Limit": limit}
        rows = self._call("POST", "/tools/find", json=body).json()
        out = [self.tag(_from_tags(r.get("MainDicomTags") or {},
                                   r.get("PatientMainDicomTags") or {}, r.get("ID")))
               for r in rows]
        messages.log(direction="out", protocol="orthanc", message_type="tools/find",
                     peer=self.label, status="ok", payload=f"{len(out)} studies")
        return out

    def _orthanc_id(self, study_uid: str) -> str:
        hits = self._call("POST", "/tools/lookup", content=study_uid).json()
        for h in hits:
            if h.get("Type") == "Study":
                return h["ID"]
        raise AdapterError(f"{self.label}: study {study_uid} not found")

    def list_series(self, study_uid: str) -> list[dict]:
        oid = self._orthanc_id(study_uid)
        out = []
        for s in self._call("GET", f"/studies/{oid}/series").json():
            t = s.get("MainDicomTags") or {}
            out.append({"SeriesInstanceUID": t.get("SeriesInstanceUID"),
                        "Modality": t.get("Modality"), "SeriesNumber": t.get("SeriesNumber"),
                        "SeriesDescription": t.get("SeriesDescription"),
                        "NumberOfSeriesRelatedInstances": len(s.get("Instances") or [])})
        return out

    def retrieve_study(self, study_uid: str, series_uid: Optional[str] = None) -> dict:
        oid = self._orthanc_id(study_uid)
        stored = failed = 0
        for inst in self._call("GET", f"/studies/{oid}/instances").json():
            if series_uid and (inst.get("MainDicomTags") or {}).get("SeriesInstanceUID") not in (None, series_uid):
                continue
            data = self._call("GET", f"/instances/{inst['ID']}/file").content
            try:
                index.ingest(data, source=f"orthanc:{self.label}",
                             origin_facility=self.node.get("facility_oid"),
                             issuer_hint=self.node.get("issuer"))
                stored += 1
            except index.IngestError:
                failed += 1
        messages.log(direction="in", protocol="orthanc", message_type="instances/file",
                     peer=self.label, status="ok" if not failed else "partial",
                     payload=f"study {study_uid}: stored={stored} failed={failed}")
        return {"stored": stored, "failed": failed}

    def send_study(self, study_uid: str) -> dict:
        from pacs.storage import get_storage
        st = get_storage()
        rows = index.instance_paths(study_uid=study_uid)
        if not rows:
            raise AdapterError(f"study {study_uid} has no local instances")
        sent = failed = 0
        for x in rows:
            try:
                self._call("POST", "/instances", content=st.get(x["path"]),
                           headers={"Content-Type": "application/dicom"})
                sent += 1
            except AdapterError:
                failed += 1
        messages.log(direction="out", protocol="orthanc", message_type="POST /instances",
                     peer=self.label, status="ok" if not failed else "partial",
                     payload=f"study {study_uid}: sent={sent} failed={failed}")
        return {"sent": sent, "failed": failed}
