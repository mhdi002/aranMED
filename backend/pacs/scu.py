"""DICOM DIMSE service user: talk *to* modalities and other PACS.

Thin, synchronous wrappers over pynetdicom (callers run them in a worker
thread). Every call opens one association, does one job, releases it, and
logs the exchange in the interop message log.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from pydicom.dataset import Dataset

from clinicaldb import messages
from pacs import config, index

log = logging.getLogger("pacs.scu")


class ScuError(RuntimeError):
    pass


def _ae():
    from pynetdicom import AE
    ae = AE(ae_title=config.ae_title())
    t = config.network_timeout()
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = t
    return ae


def _assoc(ae, node: dict, **kw):
    from pacs import nodes
    ctx = nodes.tls_context(node)
    if ctx is not None:
        kw.setdefault("tls_args", (ctx, None))
    assoc = ae.associate(node["host"], int(node["port"]), ae_title=node["ae_title"], **kw)
    if not assoc.is_established:
        raise ScuError(f"association with {node['ae_title']}@{node['host']}:{node['port']} "
                       "was rejected or failed")
    return assoc


def _charset(node: dict, values: Iterable[Any]) -> Optional[str]:
    """SpecificCharacterSet for a query: the node's configured one, else
    UTF-8 whenever a filter value is not plain ASCII (Persian names)."""
    if node.get("charset"):
        return node["charset"]
    return "ISO_IR 192" if any(isinstance(v, str) and not v.isascii() for v in values) else None


def echo(node: dict) -> bool:
    from pynetdicom.sop_class import Verification
    ae = _ae()
    ae.add_requested_context(Verification)
    try:
        assoc = _assoc(ae, node)
    except ScuError:
        return False
    try:
        status = assoc.send_c_echo()
        return bool(status) and status.Status == 0x0000
    finally:
        assoc.release()


def find(node: dict, level: str, filters: dict[str, Any],
         return_keys: Optional[Iterable[str]] = None, *, model: str = "study") -> list[dict]:
    """C-FIND at *level* (PATIENT/STUDY/SERIES/IMAGE) → list of keyword dicts."""
    from pynetdicom.sop_class import (PatientRootQueryRetrieveInformationModelFind,
                                      StudyRootQueryRetrieveInformationModelFind)
    sop = (PatientRootQueryRetrieveInformationModelFind if model == "patient"
           else StudyRootQueryRetrieveInformationModelFind)
    ae = _ae()
    ae.add_requested_context(sop)
    ds = Dataset()
    cs = _charset(node, (filters or {}).values())
    if cs:
        ds.SpecificCharacterSet = cs
    ds.QueryRetrieveLevel = level.upper()
    for k in return_keys or []:
        setattr(ds, k, "")
    for k, v in (filters or {}).items():
        setattr(ds, k, v)
    assoc = _assoc(ae, node)
    out: list[dict] = []
    try:
        for status, ident in assoc.send_c_find(ds, sop):
            if status and status.Status in (0xFF00, 0xFF01) and ident is not None:
                d = {}
                for elem in ident:
                    if elem.keyword and elem.VR != "SQ":
                        v = elem.value
                        if v.__class__.__name__ == "MultiValue":
                            v = list(map(str, v))
                        elif v is not None:
                            v = str(v)
                        d[elem.keyword] = v
                out.append(d)
    finally:
        assoc.release()
    messages.log(direction="out", protocol="dicom", message_type="C-FIND",
                 peer=node.get("ae_title"), status="ok", payload=f"{level}: {len(out)} match(es)")
    return out


def mwl_find(node: dict, *, modality: Optional[str] = None,
             station_ae: Optional[str] = None, date: Optional[str] = None,
             patient_name: Optional[str] = None) -> list[Dataset]:
    from pynetdicom.sop_class import ModalityWorklistInformationFind
    ae = _ae()
    ae.add_requested_context(ModalityWorklistInformationFind)
    ds = Dataset()
    cs = _charset(node, [patient_name])
    if cs:
        ds.SpecificCharacterSet = cs
    ds.PatientName = patient_name or ""
    ds.PatientID = ""
    ds.AccessionNumber = ""
    ds.StudyInstanceUID = ""
    item = Dataset()
    item.Modality = modality or ""
    item.ScheduledStationAETitle = station_ae or ""
    item.ScheduledProcedureStepStartDate = date or ""
    item.ScheduledProcedureStepID = ""
    ds.ScheduledProcedureStepSequence = [item]
    assoc = _assoc(ae, node)
    out = []
    try:
        for status, ident in assoc.send_c_find(ds, ModalityWorklistInformationFind):
            if status and status.Status in (0xFF00, 0xFF01) and ident is not None:
                out.append(ident)
    finally:
        assoc.release()
    return out


def move(node: dict, *, study_uid: str, series_uid: Optional[str] = None,
         destination_ae: Optional[str] = None) -> dict:
    """Ask *node* to C-MOVE a study (or series) to *destination_ae* (default: us)."""
    from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelMove
    ae = _ae()
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelMove)
    ds = Dataset()
    ds.QueryRetrieveLevel = "SERIES" if series_uid else "STUDY"
    ds.StudyInstanceUID = study_uid
    if series_uid:
        ds.SeriesInstanceUID = series_uid
    assoc = _assoc(ae, node)
    final = None
    try:
        for status, _ in assoc.send_c_move(ds, destination_ae or config.ae_title(),
                                           StudyRootQueryRetrieveInformationModelMove):
            if status:
                final = status
    finally:
        assoc.release()
    if final is None:
        raise ScuError("C-MOVE got no response")
    result = {"status": int(final.Status),
              "completed": int(final.get("NumberOfCompletedSuboperations", 0) or 0),
              "failed": int(final.get("NumberOfFailedSuboperations", 0) or 0),
              "warning": int(final.get("NumberOfWarningSuboperations", 0) or 0)}
    messages.log(direction="out", protocol="dicom", message_type="C-MOVE",
                 peer=node.get("ae_title"), status="ok" if final.Status in (0x0000, 0xB000) else "error",
                 payload=f"study {study_uid} -> {destination_ae or config.ae_title()}: {result}")
    if final.Status not in (0x0000, 0xB000):
        raise ScuError(f"C-MOVE failed with status 0x{int(final.Status):04X}")
    return result


def _study_sop_classes(node: dict, study_uid: str, series_uid: Optional[str]) -> list[str]:
    """SOP classes present in the study, asked with an IMAGE-level C-FIND, so
    C-GET can offer exactly those (any of the ~170 storage classes, or a
    vendor-private one) instead of a fixed subset."""
    filters = {"StudyInstanceUID": study_uid}
    if series_uid:
        filters["SeriesInstanceUID"] = series_uid
    try:
        rows = find(node, "IMAGE", filters, ["SOPClassUID", "SeriesInstanceUID", "SOPInstanceUID"])
    except ScuError:
        return []
    return list(dict.fromkeys(r["SOPClassUID"] for r in rows if r.get("SOPClassUID")))


def get(node: dict, *, study_uid: str, series_uid: Optional[str] = None,
        sop_classes: Optional[list[str]] = None) -> dict:
    """C-GET a study from *node*; received objects are ingested locally.

    Every storage context offers all transfer syntaxes (compressed ones
    included), so the sender never has to transcode or drop an object."""
    from pynetdicom import ALL_TRANSFER_SYNTAXES, StoragePresentationContexts, build_role, evt
    from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelGet
    ae = _ae()
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelGet)
    classes = sop_classes or _study_sop_classes(node, study_uid, series_uid) or \
        [cx.abstract_syntax for cx in StoragePresentationContexts]
    roles = []
    for uid in classes[:127]:  # 128 presentation contexts per association
        ae.add_requested_context(uid, ALL_TRANSFER_SYNTAXES)
        roles.append(build_role(uid, scp_role=True))
    stored: list[str] = []
    failed: list[str] = []

    def on_store(event):
        ds = event.dataset
        ds.file_meta = event.file_meta
        try:
            out = index.ingest(ds, source=f"c-get:{node.get('ae_title')}",
                               origin_facility=node.get("facility_oid"),
                               issuer_hint=node.get("issuer"))
            stored.append(out["sop_uid"])
            return 0x0000
        except Exception:  # noqa: BLE001
            log.exception("C-GET store failed")
            failed.append(str(getattr(ds, "SOPInstanceUID", "")))
            return 0xA700

    ds = Dataset()
    ds.QueryRetrieveLevel = "SERIES" if series_uid else "STUDY"
    ds.StudyInstanceUID = study_uid
    if series_uid:
        ds.SeriesInstanceUID = series_uid
    assoc = _assoc(ae, node, ext_neg=roles, evt_handlers=[(evt.EVT_C_STORE, on_store)])
    final = None
    try:
        for status, _ in assoc.send_c_get(ds, StudyRootQueryRetrieveInformationModelGet):
            if status:
                final = status
    finally:
        assoc.release()
    messages.log(direction="out", protocol="dicom", message_type="C-GET",
                 peer=node.get("ae_title"), status="ok" if not failed else "partial",
                 payload=f"study {study_uid}: stored={len(stored)} failed={len(failed)}")
    return {"status": int(final.Status) if final else None, "stored": len(stored),
            "failed": len(failed)}


def store(node: dict, datasets_or_bytes: Iterable) -> dict:
    """C-STORE objects (Part 10 bytes or Datasets) to *node*.

    Objects are sent over as many associations as their SOP classes need
    (128 presentation contexts each). An object whose compressed transfer
    syntax the receiver did not accept is decompressed to Explicit VR
    Little Endian rather than dropped."""
    import io

    import pydicom
    from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
    from pacs.dimse import _accepted_map, _fit
    items = []
    for x in datasets_or_bytes:
        items.append(pydicom.dcmread(io.BytesIO(x), force=True) if isinstance(x, (bytes, bytearray))
                     else x)
    if not items:
        return {"sent": 0, "failed": 0}

    def ts_of(ds) -> str:
        return str(ds.file_meta.TransferSyntaxUID) if getattr(ds, "file_meta", None) \
            else ExplicitVRLittleEndian
    by_class: dict[str, list] = {}
    for ds in items:
        by_class.setdefault(str(ds.SOPClassUID), []).append(ds)
    groups, cur = [], []
    for cls, dss in by_class.items():
        n_ctx = len({ts_of(d) for d in dss})
        if cur and sum(len({ts_of(d) for d in g[1]}) for g in cur) + n_ctx > 126:
            groups.append(cur)
            cur = []
        cur.append((cls, dss))
    if cur:
        groups.append(cur)
    sent = failed = 0
    for group in groups:
        ae = _ae()
        for cls, dss in group:
            for ts in dict.fromkeys(ts_of(d) for d in dss):
                ae.add_requested_context(cls, list(dict.fromkeys([ts, ExplicitVRLittleEndian,
                                                                  ImplicitVRLittleEndian])))
        assoc = _assoc(ae, node)
        accepted = _accepted_map(assoc.accepted_contexts)
        try:
            for _cls, dss in group:
                for ds in dss:
                    try:
                        status = assoc.send_c_store(_fit(ds, accepted.get(str(ds.SOPClassUID), set())))
                    except ValueError as e:  # nothing acceptable negotiated for this object
                        log.warning("C-STORE of %s skipped: %s", getattr(ds, "SOPInstanceUID", "?"), e)
                        status = None
                    if status and status.Status in (0x0000, 0xB000, 0xB007, 0xB006):
                        sent += 1
                    else:
                        failed += 1
        finally:
            assoc.release()
    messages.log(direction="out", protocol="dicom", message_type="C-STORE",
                 peer=node.get("ae_title"), status="ok" if not failed else "partial",
                 payload=f"sent={sent} failed={failed}")
    return {"sent": sent, "failed": failed}


def store_study(node: dict, study_uid: str) -> dict:
    from pacs.storage import get_storage
    st = get_storage()
    blobs = [st.get(i["path"]) for i in index.instance_paths(study_uid=study_uid)]
    if not blobs:
        raise ScuError(f"study {study_uid} has no stored instances")
    return store(node, blobs)
