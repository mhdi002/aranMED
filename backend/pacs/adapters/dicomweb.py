"""Generic DICOMweb adapter (dcm4chee-arc, Orthanc DICOMweb plugin, cloud
healthcare APIs, another AranMed)."""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from clinicaldb import facilities, messages
from pacs import config, index, multipart, nodes
from pacs.adapters.base import AdapterError, RemotePacs, keyword_dict_from_json

log = logging.getLogger("pacs.adapters.dicomweb")


class DicomwebAdapter(RemotePacs):
    def _headers(self, purpose: str = "TREAT") -> dict[str, str]:
        # An AranMed peer facility gets a signed peer token; any other
        # DICOMweb server uses the node's configured credential.
        oid = self.node.get("facility_oid")
        fac = facilities.get_by_oid(oid) if oid else None
        if fac and not fac.get("is_local") and facilities.peer_secret(fac):
            from clinicaldb import principal
            return {"Authorization": "Bearer " + principal.token_for_peer(
                fac, practitioner=self.node.get("_practitioner") or "system", purpose=purpose)}
        return nodes.auth_header(self.node)

    @property
    def base(self) -> str:
        return (self.node.get("base_url") or "").rstrip("/")

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=config.http_timeout(), headers=self._headers())

    def echo(self) -> bool:
        try:
            with self._client() as c:
                r = c.get(f"{self.base}/studies", params={"limit": 1},
                          headers={"Accept": "application/dicom+json"})
            return r.status_code in (200, 204)
        except httpx.HTTPError:
            return False

    def _qido(self, path: str, params: dict) -> list[dict]:
        try:
            with self._client() as c:
                r = c.get(f"{self.base}{path}", params=params,
                          headers={"Accept": "application/dicom+json"})
        except httpx.HTTPError as e:
            raise AdapterError(f"{self.label}: {e}") from e
        if r.status_code == 204:
            return []
        if r.status_code != 200:
            raise AdapterError(f"{self.label}: QIDO {path} -> HTTP {r.status_code}")
        return [keyword_dict_from_json(x) for x in r.json()]

    def search_studies(self, filters: dict[str, Any], limit: int = 100) -> list[dict]:
        params = {k: v for k, v in filters.items() if not k.startswith("x-") and v}
        params["limit"] = limit
        params["includefield"] = "all"
        out = [self.tag(s) for s in self._qido("/studies", params)]
        messages.log(direction="out", protocol="dicomweb", message_type="QIDO-RS",
                     peer=self.label, status="ok", payload=f"{len(out)} studies")
        return out

    def list_series(self, study_uid: str) -> list[dict]:
        return self._qido(f"/studies/{study_uid}/series", {})

    def retrieve_study(self, study_uid: str, series_uid: Optional[str] = None) -> dict:
        path = f"/studies/{study_uid}" + (f"/series/{series_uid}" if series_uid else "")
        try:
            with self._client() as c:
                r = c.get(f"{self.base}{path}",
                          headers={"Accept": 'multipart/related; type="application/dicom"'})
        except httpx.HTTPError as e:
            raise AdapterError(f"{self.label}: {e}") from e
        if r.status_code != 200:
            raise AdapterError(f"{self.label}: WADO-RS {path} -> HTTP {r.status_code}")
        bnd = multipart.parse_boundary(r.headers.get("content-type", ""))
        if not bnd:
            raise AdapterError(f"{self.label}: WADO-RS response is not multipart")
        stored = failed = 0
        for _headers, part in multipart.decode(r.content, bnd):
            try:
                index.ingest(part, source=f"wado:{self.label}",
                             origin_facility=self.node.get("facility_oid"),
                             issuer_hint=self.node.get("issuer"))
                stored += 1
            except index.IngestError as e:
                log.warning("retrieve %s: part rejected: %s", study_uid, e)
                failed += 1
        messages.log(direction="in", protocol="dicomweb", message_type="WADO-RS",
                     peer=self.label, status="ok" if not failed else "partial",
                     payload=f"study {study_uid}: stored={stored} failed={failed}")
        return {"stored": stored, "failed": failed}

    def send_study(self, study_uid: str) -> dict:
        from pacs.storage import get_storage
        st = get_storage()
        rows = index.instance_paths(study_uid=study_uid)
        if not rows:
            raise AdapterError(f"study {study_uid} has no local instances")
        bnd = multipart.boundary()
        body = multipart.encode(((st.get(x["path"]), "application/dicom") for x in rows), bnd)
        headers = {**self._headers(purpose=self.node.get("_purpose") or "TREAT"),
                   "Content-Type": multipart.content_type(bnd, "application/dicom"),
                   "Accept": "application/dicom+json"}
        try:
            with httpx.Client(timeout=config.http_timeout()) as c:
                r = c.post(f"{self.base}/studies", content=body, headers=headers)
        except httpx.HTTPError as e:
            raise AdapterError(f"{self.label}: {e}") from e
        if r.status_code not in (200, 202):
            raise AdapterError(f"{self.label}: STOW-RS -> HTTP {r.status_code}: {r.text[:200]}")
        js = r.json() if r.content else {}
        sent = len((js.get("00081199") or {}).get("Value") or [])
        failed = len((js.get("00081198") or {}).get("Value") or [])
        messages.log(direction="out", protocol="dicomweb", message_type="STOW-RS",
                     peer=self.label, status="ok" if not failed else "partial",
                     payload=f"study {study_uid}: sent={sent} failed={failed}")
        return {"sent": sent, "failed": failed}
