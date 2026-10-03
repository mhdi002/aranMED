"""DICOMweb: QIDO-RS, WADO-RS, STOW-RS and WADO-URI (DICOM PS3.18).

Mounted at ``DICOMWEB_PREFIX`` (default ``/api/dicom-web``, so the existing
gateway and frontend proxy route it unchanged). Both this hospital's users
(session token, ``pacs.read``/``pacs.write``) and peer facilities (signed
peer token, per ``peer_policy.json``) are accepted — the same endpoint the
in-app viewer reads is what another hospital's PACS federates against.

Implemented:

* QIDO-RS  ``GET /studies``, ``/series``, ``/instances`` and the nested
  forms, with attribute matching, ``limit``/``offset``, ``includefield``.
* WADO-RS  study / series / instance retrieval (multipart ``application/dicom``),
  ``/metadata`` (DICOM JSON, pixel data as BulkDataURI), ``/frames/{list}``
  (as stored: native or encapsulated), ``/rendered`` and ``/thumbnail``
  (PNG or JPEG, optional ``window=center,width``).
* STOW-RS  ``POST /studies`` and ``POST /studies/{study}`` with a standard
  store-response (ReferencedSOPSequence / FailedSOPSequence).
* WADO-URI ``GET /wado?requestType=WADO&studyUID=…&seriesUID=…&objectUID=…``.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response

import audit
from clinicaldb import principal as pr
from pacs import config, dicomjson, index, multipart, render
from pacs.storage import get_storage

log = logging.getLogger("pacs.dicomweb")

DICOM_JSON = "application/dicom+json"
_CONTROL = {"limit", "offset", "includefield", "fuzzymatching", "access_token"}


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/") + config.dicomweb_prefix()


def _filters(request: Request) -> tuple[dict, int, int, list[str]]:
    filters: dict[str, str] = {}
    for k, v in request.query_params.multi_items():
        if k not in _CONTROL:
            filters[k] = v
    try:
        limit = int(request.query_params.get("limit", "100"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        raise HTTPException(400, "limit/offset must be integers")
    return filters, max(1, min(limit, 5000)), max(0, offset), \
        request.query_params.getlist("includefield")


def _json(payload, status: int = 200) -> Response:
    import json
    return Response(json.dumps(payload, ensure_ascii=False), status_code=status,
                    media_type=DICOM_JSON)


def _audit(p: dict, action: str, resource: str, request: Request, **detail) -> None:
    audit.record(action, **pr.actor(p), resource=resource,
                 client_ip=request.client.host if request.client else None,
                 detail={"kind": p.get("kind"), "purpose": p.get("purpose"), **detail} or None)


def make_router() -> APIRouter:
    r = APIRouter(prefix=config.dicomweb_prefix(), tags=["dicomweb"])
    read = Depends(pr.require("pacs.read"))
    write = Depends(pr.require("pacs.write"))

    # ------------------------------------------------------------------ QIDO
    def _qido_studies(request: Request, extra: Optional[dict] = None):
        filters, limit, offset, inc = _filters(request)
        filters.update(extra or {})
        keys = dicomjson.requested_keys(dicomjson.STUDY_RETURN, inc)
        base = _base(request)
        return [dicomjson.to_json(s, keys, f"{base}/studies/{s['StudyInstanceUID']}")
                for s in index.query_studies(filters, limit=limit, offset=offset)]

    @r.get("/studies")
    def qido_studies(request: Request, p: dict = read):
        out = _qido_studies(request)
        _audit(p, "pacs.qido", "studies", request, results=len(out))
        return _json(out)

    def _qido_series(request: Request, extra: dict):
        filters, limit, offset, inc = _filters(request)
        filters.update(extra)
        keys = dicomjson.requested_keys(dicomjson.SERIES_RETURN, inc)
        base = _base(request)
        return [dicomjson.to_json(
            s, keys, f"{base}/studies/{s['StudyInstanceUID']}/series/{s['SeriesInstanceUID']}")
            for s in index.query_series(filters, limit=limit, offset=offset)]

    @r.get("/series")
    def qido_all_series(request: Request, p: dict = read):
        return _json(_qido_series(request, {}))

    @r.get("/studies/{study}/series")
    def qido_series(study: str, request: Request, p: dict = read):
        return _json(_qido_series(request, {"StudyInstanceUID": study}))

    def _qido_instances(request: Request, extra: dict):
        filters, limit, offset, inc = _filters(request)
        filters.update(extra)
        keys = dicomjson.requested_keys(dicomjson.INSTANCE_RETURN, inc)
        base = _base(request)
        return [dicomjson.to_json(
            i, keys, f"{base}/studies/{i['StudyInstanceUID']}/series/{i['SeriesInstanceUID']}"
                     f"/instances/{i['SOPInstanceUID']}")
            for i in index.query_instances(filters, limit=limit, offset=offset)]

    @r.get("/instances")
    def qido_all_instances(request: Request, p: dict = read):
        return _json(_qido_instances(request, {}))

    @r.get("/studies/{study}/instances")
    def qido_study_instances(study: str, request: Request, p: dict = read):
        return _json(_qido_instances(request, {"StudyInstanceUID": study}))

    @r.get("/studies/{study}/series/{series}/instances")
    def qido_series_instances(study: str, series: str, request: Request, p: dict = read):
        return _json(_qido_instances(request, {"StudyInstanceUID": study,
                                               "SeriesInstanceUID": series}))

    # ------------------------------------------------------------------ WADO
    def _instances(study: str, series: Optional[str] = None,
                   sop: Optional[str] = None) -> list[dict]:
        rows = index.instance_paths(study_uid=study, series_uid=series,
                                    sop_uids=[sop] if sop else None)
        if not rows:
            raise HTTPException(404, "no matching instances")
        return rows

    def _multipart_dicom(rows: list[dict]) -> Response:
        st = get_storage()
        bnd = multipart.boundary()
        body = multipart.encode(((st.get(x["path"]), "application/dicom") for x in rows), bnd)
        return Response(body, media_type=multipart.content_type(bnd, "application/dicom"))

    @r.get("/studies/{study}")
    def wado_study(study: str, request: Request, p: dict = read):
        rows = _instances(study)
        _audit(p, "pacs.wado.retrieve", f"study:{study}", request, instances=len(rows))
        return _multipart_dicom(rows)

    @r.get("/studies/{study}/series/{series}")
    def wado_series(study: str, series: str, request: Request, p: dict = read):
        rows = _instances(study, series)
        _audit(p, "pacs.wado.retrieve", f"study:{study}", request, series=series)
        return _multipart_dicom(rows)

    @r.get("/studies/{study}/series/{series}/instances/{sop}")
    def wado_instance(study: str, series: str, sop: str, request: Request,
                            p: dict = read):
        rows = _instances(study, series, sop)
        accept = request.headers.get("accept", "")
        if "application/dicom" in accept and "multipart" not in accept:
            return Response(get_storage().get(rows[0]["path"]), media_type="application/dicom")
        return _multipart_dicom(rows)

    def _metadata(rows: list[dict], request: Request) -> list[dict]:
        st = get_storage()
        base = _base(request)
        out = []
        for x in rows:
            ds = render.load(st.get(x["path"]), pixels=False)
            url = (f"{base}/studies/{x['study_uid']}/series/{x['series_uid']}"
                   f"/instances/{x['sop_uid']}")
            meta = dicomjson.dataset_metadata_json(ds, lambda tag, u=url: f"{u}/bulk/{tag}")
            if x.get("rows_"):
                meta["7FE00010"] = {"vr": "OW", "BulkDataURI": f"{url}/frames/1"}
            # Transfer syntax lives in file meta; viewers need it for frames.
            meta["00020010"] = {"vr": "UI", "Value": [x.get("transfer_syntax")
                                                      or "1.2.840.10008.1.2.1"]}
            out.append(meta)
        return out

    @r.get("/studies/{study}/metadata")
    def meta_study(study: str, request: Request, p: dict = read):
        rows = _instances(study)
        _audit(p, "pacs.wado.metadata", f"study:{study}", request)
        return _json(_metadata(rows, request))

    @r.get("/studies/{study}/series/{series}/metadata")
    def meta_series(study: str, series: str, request: Request, p: dict = read):
        rows = _instances(study, series)
        _audit(p, "pacs.wado.metadata", f"study:{study}", request, series=series)
        return _json(_metadata(rows, request))

    @r.get("/studies/{study}/series/{series}/instances/{sop}/metadata")
    def meta_instance(study: str, series: str, sop: str, request: Request,
                            p: dict = read):
        return _json(_metadata(_instances(study, series, sop), request))

    @r.get("/studies/{study}/series/{series}/instances/{sop}/frames/{frames}")
    def wado_frames(study: str, series: str, sop: str, frames: str, request: Request,
                          p: dict = read):
        x = _instances(study, series, sop)[0]
        ds = render.load(get_storage().get(x["path"]))
        try:
            numbers = [int(f) for f in frames.split(",") if f.strip()]
            data = [render.frame_bytes(ds, n) for n in numbers]
        except (ValueError, IndexError) as e:
            raise HTTPException(404, str(e)) from e
        ts = render.transfer_syntax(ds)
        part_type = (f"application/octet-stream; transfer-syntax={ts}"
                     if render.is_compressed(ds) else "application/octet-stream")
        bnd = multipart.boundary()
        body = multipart.encode(((d, part_type) for d in data), bnd)
        extra = f"; transfer-syntax={ts}" if render.is_compressed(ds) else ""
        return Response(body, media_type=multipart.content_type(
            bnd, "application/octet-stream", extra))

    @r.get("/studies/{study}/series/{series}/instances/{sop}/bulk/{tag}")
    def bulk(study: str, series: str, sop: str, tag: str, request: Request, p: dict = read):
        x = _instances(study, series, sop)[0]
        ds = render.load(get_storage().get(x["path"]))
        try:
            elem = ds[int(tag, 16)]
        except (KeyError, ValueError) as e:
            raise HTTPException(404, "no such element") from e
        bnd = multipart.boundary()
        val = elem.value if isinstance(elem.value, (bytes, bytearray)) else str(elem.value).encode()
        return Response(multipart.encode([(bytes(val), "application/octet-stream")], bnd),
                        media_type=multipart.content_type(bnd, "application/octet-stream"))

    def _window(request: Request) -> tuple[Optional[float], Optional[float]]:
        w = request.query_params.get("window")
        if not w:
            return None, None
        try:
            c, wd = w.split(",")[:2]
            return float(c), float(wd)
        except ValueError:
            raise HTTPException(400, "window must be center,width[,function]")

    def _rendered(x: dict, frame: int, request: Request, max_size: int = 2048) -> Response:
        accept = request.headers.get("accept", "")
        fmt = "jpeg" if "jpeg" in accept and "png" not in accept else "png"
        wc, ww = _window(request)
        try:
            img = render.render(get_storage().get(x["path"]), frame=frame, window_center=wc,
                                window_width=ww, fmt=fmt, max_size=max_size)
        except ValueError as e:
            raise HTTPException(406, str(e)) from e
        return Response(img, media_type=f"image/{fmt}")

    @r.get("/studies/{study}/series/{series}/instances/{sop}/rendered")
    def rendered_instance(study: str, series: str, sop: str, request: Request,
                                p: dict = read):
        return _rendered(_instances(study, series, sop)[0], 1, request)

    @r.get("/studies/{study}/series/{series}/instances/{sop}/frames/{frame}/rendered")
    def rendered_frame(study: str, series: str, sop: str, frame: int,
                             request: Request, p: dict = read):
        return _rendered(_instances(study, series, sop)[0], frame, request)

    def _thumb(rows: list[dict], request: Request) -> Response:
        imgs = [x for x in rows if x.get("rows_")]
        if not imgs:
            raise HTTPException(404, "no image instances")
        x = imgs[len(imgs) // 2]
        size = int(request.query_params.get("size", "128"))
        return _rendered(x, max(1, (x.get("frames") or 1) // 2 or 1), request,
                         max_size=max(32, min(size, 512)))

    @r.get("/studies/{study}/thumbnail")
    def thumb_study(study: str, request: Request, p: dict = read):
        return _thumb(_instances(study), request)

    @r.get("/studies/{study}/series/{series}/thumbnail")
    def thumb_series(study: str, series: str, request: Request, p: dict = read):
        return _thumb(_instances(study, series), request)

    @r.get("/studies/{study}/series/{series}/instances/{sop}/thumbnail")
    def thumb_instance(study: str, series: str, sop: str, request: Request,
                             p: dict = read):
        return _thumb(_instances(study, series, sop), request)

    # ------------------------------------------------------------------ STOW
    async def _stow(request: Request, p: dict, study: Optional[str]) -> Response:
        from starlette.concurrency import run_in_threadpool
        body = await request.body()
        return await run_in_threadpool(_stow_sync, request, p, study, body)

    def _stow_sync(request: Request, p: dict, study: Optional[str], body: bytes) -> Response:
        ctype = request.headers.get("content-type", "")
        bnd = multipart.parse_boundary(ctype)
        if len(body) > config.max_upload_mb() * 1024 * 1024:
            raise HTTPException(413, "payload too large")
        if bnd:
            parts = [payload for headers, payload in multipart.decode(body, bnd)
                     if "dicom" in headers.get("content-type", "application/dicom")]
        elif "application/dicom" in ctype:
            parts = [body]
        else:
            raise HTTPException(415, 'expected multipart/related; type="application/dicom"')
        from pydicom.dataset import Dataset
        resp = Dataset()
        resp.RetrieveURL = f"{_base(request)}/studies/{study}" if study else ""
        ok, failed = [], []
        origin = p.get("facility_oid") if p.get("kind") == "peer" else None
        for blob in parts:
            meta = None
            try:
                meta = index._read(blob)  # noqa: SLF001
                if study and str(meta.get("StudyInstanceUID")) != study:
                    raise index.IngestError("StudyInstanceUID does not match the target study")
                out = index.ingest(blob, source=f"stow:{p.get('username')}",
                                   origin_facility=origin)
                item = Dataset()
                item.ReferencedSOPClassUID = str(meta.SOPClassUID)
                item.ReferencedSOPInstanceUID = out["sop_uid"]
                item.RetrieveURL = (f"{_base(request)}/studies/{out['study_uid']}/series/"
                                    f"{out['series_uid']}/instances/{out['sop_uid']}")
                ok.append(item)
            except index.IngestError as e:
                item = Dataset()
                if meta is not None and "SOPInstanceUID" in meta:
                    item.ReferencedSOPClassUID = str(meta.get("SOPClassUID", ""))
                    item.ReferencedSOPInstanceUID = str(meta.SOPInstanceUID)
                item.FailureReason = 0xA900 if "match" in str(e) else 0xC000
                failed.append(item)
                log.warning("STOW part rejected: %s", e)
        if ok:
            resp.ReferencedSOPSequence = ok
        if failed:
            resp.FailedSOPSequence = failed
        _audit(p, "pacs.stow", f"study:{study or '*'}", request, stored=len(ok),
               failed=len(failed))
        status = 200 if not failed else (202 if ok else 409)
        return _json(resp.to_json_dict(), status)

    @r.post("/studies")
    async def stow(request: Request, p: dict = write):
        return await _stow(request, p, None)

    @r.post("/studies/{study}")
    async def stow_study(study: str, request: Request, p: dict = write):
        return await _stow(request, p, study)

    # -------------------------------------------------------------- WADO-URI
    @r.get("/wado")
    def wado_uri(request: Request, p: dict = read):
        q = request.query_params
        if q.get("requestType") != "WADO":
            raise HTTPException(400, "requestType must be WADO")
        rows = _instances(q.get("studyUID", ""), q.get("seriesUID") or None,
                          q.get("objectUID") or None)
        x = rows[0]
        ctype = q.get("contentType", "image/jpeg")
        if ctype == "application/dicom":
            return Response(get_storage().get(x["path"]), media_type="application/dicom")
        fmt = "png" if "png" in ctype else "jpeg"
        wc = q.get("windowCenter")
        ww = q.get("windowWidth")
        img = render.render(get_storage().get(x["path"]), frame=int(q.get("frameNumber", "1")),
                            window_center=float(wc) if wc else None,
                            window_width=float(ww) if ww else None, fmt=fmt)
        return Response(img, media_type=f"image/{fmt}")

    return r
