"""A minimal Orthanc REST server for adapter tests.

Implements just the Orthanc endpoints ``pacs.adapters.orthanc`` uses, backed
by in-memory DICOM objects, and serves them over real HTTP (uvicorn thread).
Response shapes follow Orthanc's documented REST API.
"""
from __future__ import annotations

import hashlib
import io
import threading
import time

import pydicom
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response


def _oid(uid: str) -> str:
    return hashlib.sha1(uid.encode()).hexdigest()[:24]


class FakeOrthanc:
    def __init__(self, port: int, user: str = "orthanc", password: str = "orthanc") -> None:
        self.port = port
        self.instances: dict[str, bytes] = {}   # orthanc instance id -> file
        self.auth = (user, password)
        self.app = self._build()
        self._server = None

    def add(self, data: bytes) -> None:
        ds = pydicom.dcmread(io.BytesIO(data), stop_before_pixels=True)
        self.instances[_oid(str(ds.SOPInstanceUID))] = data

    def _studies(self) -> dict[str, list]:
        out: dict[str, list] = {}
        for iid, data in self.instances.items():
            ds = pydicom.dcmread(io.BytesIO(data), stop_before_pixels=True)
            out.setdefault(str(ds.StudyInstanceUID), []).append((iid, ds))
        return out

    def _build(self) -> FastAPI:
        app = FastAPI()

        def check(request: Request) -> None:
            import base64
            h = request.headers.get("authorization", "")
            want = "Basic " + base64.b64encode(f"{self.auth[0]}:{self.auth[1]}".encode()).decode()
            if h != want:
                raise HTTPException(401, "unauthorized")

        @app.get("/system")
        def system(request: Request):
            check(request)
            return {"Name": "FakeOrthanc", "Version": "1.12.0", "DicomAet": "ORTHANC"}

        @app.post("/tools/find")
        async def find(request: Request):
            check(request)
            body = await request.json()
            q = body.get("Query", {})
            res = []
            for uid, items in self._studies().items():
                ds = items[0][1]
                ok = True
                for k, v in q.items():
                    val = str(ds.get(k, ""))
                    # Orthanc matches PN/LO wildcards case-insensitively.
                    import fnmatch
                    if "*" in v or "?" in v:
                        ok &= fnmatch.fnmatch(val.lower(), v.lower())
                    else:
                        ok &= val == v
                if not ok:
                    continue
                mods = sorted({str(d.Modality) for _, d in items})
                res.append({"ID": _oid(uid), "Type": "Study",
                            "MainDicomTags": {"StudyInstanceUID": uid,
                                              "StudyDate": str(ds.get("StudyDate", "")),
                                              "StudyDescription": str(ds.get("StudyDescription", "")),
                                              "AccessionNumber": str(ds.get("AccessionNumber", "")),
                                              "ModalitiesInStudy": "\\".join(mods)},
                            "PatientMainDicomTags": {"PatientName": str(ds.PatientName),
                                                     "PatientID": str(ds.PatientID)}})
            return res[: body.get("Limit") or None]

        @app.post("/tools/lookup")
        async def lookup(request: Request):
            check(request)
            uid = (await request.body()).decode().strip()
            if uid in self._studies():
                return [{"ID": _oid(uid), "Type": "Study", "Path": f"/studies/{_oid(uid)}"}]
            return []

        def study_items(sid: str):
            for uid, items in self._studies().items():
                if _oid(uid) == sid:
                    return items
            raise HTTPException(404, "unknown study")

        @app.get("/studies/{sid}/instances")
        def instances(sid: str, request: Request):
            check(request)
            return [{"ID": iid, "Type": "Instance",
                     "MainDicomTags": {"SOPInstanceUID": str(ds.SOPInstanceUID),
                                       "SeriesInstanceUID": str(ds.SeriesInstanceUID)}}
                    for iid, ds in study_items(sid)]

        @app.get("/studies/{sid}/series")
        def series(sid: str, request: Request):
            check(request)
            by: dict[str, list] = {}
            for iid, ds in study_items(sid):
                by.setdefault(str(ds.SeriesInstanceUID), []).append((iid, ds))
            return [{"ID": _oid(k), "Instances": [i for i, _ in v],
                     "MainDicomTags": {"SeriesInstanceUID": k, "Modality": str(v[0][1].Modality),
                                       "SeriesNumber": str(v[0][1].SeriesNumber)}}
                    for k, v in by.items()]

        @app.get("/instances/{iid}/file")
        def file(iid: str, request: Request):
            check(request)
            if iid not in self.instances:
                raise HTTPException(404)
            return Response(self.instances[iid], media_type="application/dicom")

        @app.post("/instances")
        async def upload(request: Request):
            check(request)
            data = await request.body()
            self.add(data)
            return {"Status": "Success"}

        return app

    def start(self) -> "FakeOrthanc":
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(cfg)
        threading.Thread(target=self._server.run, daemon=True).start()
        deadline = time.time() + 10
        while not self._server.started and time.time() < deadline:
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
