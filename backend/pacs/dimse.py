"""DICOM DIMSE service provider (the PACS's network face to modalities/PACS).

Services (all on one AE / port, configured by ``PACS_AE_TITLE`` /
``PACS_DIMSE_PORT``):

* Verification            C-ECHO
* Storage                 C-STORE for every storage SOP class, any transfer syntax
* Query/Retrieve          C-FIND / C-MOVE / C-GET, Patient Root and Study Root
* Modality Worklist       C-FIND (MWL)
* MPPS                    N-CREATE / N-SET
* Storage Commitment      N-ACTION, answered by an N-EVENT-REPORT on a new
                          association back to the requesting node

Access control: with ``PACS_REQUIRE_KNOWN_PEERS`` (default on) the calling
AE title must be an active row in ``pacs_nodes``; its ``allow_store`` /
``allow_query`` / ``allow_retrieve`` flags then gate each operation, and a
C-MOVE destination must be a registered node flagged ``is_move_destination``
— a PACS that will send studies to any AE title a caller names is a data
exfiltration channel. Optional TLS via ``PACS_DIMSE_TLS_CERT`` /
``PACS_DIMSE_TLS_KEY`` (/ ``PACS_DIMSE_TLS_CA`` to require client certs).
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Iterable, Optional

from pydicom.dataset import Dataset

from clinicaldb import messages, settings
from pacs import config, index, nodes, worklist
from pacs.dicomjson import (INSTANCE_RETURN, PATIENT_RETURN, SERIES_RETURN, STUDY_RETURN,
                            to_dataset)

log = logging.getLogger("pacs.dimse")

# Status codes (PS3.4 / PS3.7)
SUCCESS = 0x0000
PENDING = 0xFF00
CANCEL = 0xFE00
NOT_AUTHORIZED = 0x0124
OUT_OF_RESOURCES = 0xA700
CANNOT_UNDERSTAND = 0xC000
MOVE_DEST_UNKNOWN = 0xA801
IDENTIFIER_MISMATCH = 0xA900
PROCESSING_FAILURE = 0x0110
DUPLICATE_SOP_INSTANCE = 0x0111
NO_SUCH_OBJECT = 0x0112

_SKIP_FILTER = {"QueryRetrieveLevel", "SpecificCharacterSet", "RetrieveAETitle",
                "InstanceAvailability"}


def _calling(event) -> str:
    return str(event.assoc.requestor.ae_title).strip()


def _node_for(event, permission: Optional[str]) -> tuple[Optional[dict], bool]:
    """Return (node, allowed) for the calling AE."""
    node = nodes.by_ae(_calling(event))
    if not config.require_known_peers():
        return node, True if not node or not permission else bool(node.get(permission))
    if not node:
        return None, False
    return node, bool(node.get(permission)) if permission else True


def _filters(identifier: Dataset) -> tuple[dict[str, str], list[str]]:
    filters: dict[str, str] = {}
    keys: list[str] = []
    for elem in identifier:
        kw = elem.keyword
        if not kw or kw in _SKIP_FILTER:
            continue
        keys.append(kw)
        if elem.VR == "SQ":
            continue
        v = elem.value
        if v is None or v == "":
            continue
        if elem.VM > 1 or v.__class__.__name__ == "MultiValue":
            v = "\\".join(str(x) for x in v)
        filters[kw] = str(v)
    return filters, keys


def _peer_facility(node: Optional[dict]) -> Optional[str]:
    """The other hospital a node belongs to, if any. Its patients' sharing
    consent then applies to DIMSE queries and retrievals exactly as it does
    to DICOMweb and FHIR."""
    oid = (node or {}).get("facility_oid")
    return oid if oid and oid != settings.facility_oid() else None


def _allowed_studies(node: Optional[dict], study_uids: Iterable[str]) -> Optional[set[str]]:
    oid = _peer_facility(node)
    if not oid:
        return None  # local modality / workstation: no consent filter
    from ehr import access
    from pacs.dicomweb import _person_of_studies
    allowed = set()
    for uid, person in _person_of_studies(sorted(set(study_uids))).items():
        ok, why = (True, "unmatched") if not person else access.peer_read_decision(person, oid, "TREAT")
        if ok:
            allowed.add(uid)
        else:
            import audit
            audit.record("pacs.access", actor_name=f"dimse:{(node or {}).get('ae_title')}",
                         resource=f"study:{uid}", outcome="deny", detail={"why": why, "facility": oid})
    return allowed


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def handle_echo(event) -> int:
    return SUCCESS


def handle_store(event) -> int:
    node, ok = _node_for(event, "allow_store")
    calling = _calling(event)
    if not ok:
        log.warning("C-STORE refused from %s (not authorised)", calling)
        messages.log(direction="in", protocol="dicom", message_type="C-STORE", peer=calling,
                     status="rejected", error="calling AE not authorised to store")
        return NOT_AUTHORIZED
    try:
        ds = event.dataset
        ds.file_meta = event.file_meta
        out = index.ingest(ds, source=f"dimse:{calling}",
                           origin_facility=(node or {}).get("facility_oid"),
                           issuer_hint=(node or {}).get("issuer"))
    except index.IngestError as e:
        log.warning("C-STORE from %s rejected: %s", calling, e)
        return CANNOT_UNDERSTAND
    except OSError:
        log.exception("C-STORE storage failure")
        return OUT_OF_RESOURCES
    except Exception:  # noqa: BLE001
        log.exception("C-STORE processing failure")
        return PROCESSING_FAILURE
    log.debug("C-STORE %s %s from %s", out["status"], out["sop_uid"], calling)
    return SUCCESS


def _query(level: str, filters: dict[str, str]) -> tuple[list[dict], list[str]]:
    level = (level or "STUDY").upper()
    if level == "PATIENT":
        return index.query_patients(filters), PATIENT_RETURN
    if level == "SERIES":
        return index.query_series(filters), SERIES_RETURN
    if level == "IMAGE":
        return index.query_instances(filters), INSTANCE_RETURN
    return index.query_studies(filters, limit=5000), STUDY_RETURN


def handle_find(event):
    from pynetdicom.sop_class import ModalityWorklistInformationFind

    node, ok = _node_for(event, "allow_query")
    if not ok:
        yield NOT_AUTHORIZED, None
        return
    identifier = event.identifier
    if event.request.AffectedSOPClassUID == ModalityWorklistInformationFind:
        for ds in worklist.mwl_query(identifier, charset=(node or {}).get("charset") or None):
            if event.is_cancelled:
                yield CANCEL, None
                return
            yield PENDING, ds
        return

    level = str(identifier.get("QueryRetrieveLevel") or "STUDY").upper()
    if level not in ("PATIENT", "STUDY", "SERIES", "IMAGE"):
        yield IDENTIFIER_MISMATCH, None
        return
    filters, keys = _filters(identifier)
    results, _default = _query(level, filters)
    if level != "PATIENT":
        allowed = _allowed_studies(node, [r.get("StudyInstanceUID") for r in results
                                          if r.get("StudyInstanceUID")])
        if allowed is not None:
            results = [r for r in results if r.get("StudyInstanceUID") in allowed]
    elif _peer_facility(node):
        def visible(p: dict) -> bool:
            studies = index.query_studies({"PatientID": p.get("PatientID")}, limit=500)
            return bool(_allowed_studies(node, [x["StudyInstanceUID"] for x in studies]))
        results = [r for r in results if visible(r)]
    for item in results:
        if event.is_cancelled:
            yield CANCEL, None
            return
        ds = to_dataset(item, keys)
        ds.QueryRetrieveLevel = level
        ds.RetrieveAETitle = config.ae_title()
        yield PENDING, ds


def _matching_instances(identifier: Dataset) -> list[dict]:
    level = str(identifier.get("QueryRetrieveLevel") or "STUDY").upper()
    filters, _ = _filters(identifier)
    if level == "PATIENT" and filters.get("PatientID"):
        studies = index.query_studies({"PatientID": filters["PatientID"]}, limit=5000)
        out: list[dict] = []
        for s in studies:
            out += index.instance_paths(study_uid=s["StudyInstanceUID"])
        return out
    sop = filters.get("SOPInstanceUID")
    if level == "IMAGE" and sop:
        return index.instance_paths(sop_uids=sop.replace("\\", ",").split(","))
    series = filters.get("SeriesInstanceUID")
    if level in ("SERIES", "IMAGE") and series:
        out = []
        for s in series.replace("\\", ",").split(","):
            out += index.instance_paths(series_uid=s)
        return out
    study = filters.get("StudyInstanceUID")
    if study:
        out = []
        for s in study.replace("\\", ",").split(","):
            out += index.instance_paths(study_uid=s)
        return out
    return []


def _consented(node: Optional[dict], instances: list[dict]) -> list[dict]:
    allowed = _allowed_studies(node, [i["study_uid"] for i in instances])
    return instances if allowed is None else [i for i in instances if i["study_uid"] in allowed]


def _datasets(instances: Iterable[dict], accepted: Optional[dict[str, set[str]]] = None):
    """Yield stored objects; when *accepted* (SOP class -> transfer syntaxes the
    receiver accepted) says the receiver cannot take the stored compressed
    syntax, decompress to Explicit VR Little Endian first."""
    from pacs.render import load
    from pacs.storage import get_storage
    st = get_storage()
    for inst in instances:
        ds = load(st.get(inst["path"]))
        if accepted is not None:
            ds = _fit(ds, accepted.get(str(ds.SOPClassUID), set()))
        yield ds


def _fit(ds, accepted: set[str]):
    ts = str(ds.file_meta.TransferSyntaxUID)
    if ts in accepted or not ds.file_meta.TransferSyntaxUID.is_compressed:
        return ds
    from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
    if accepted & {ExplicitVRLittleEndian, ImplicitVRLittleEndian} or not accepted:
        try:
            # Same object, different encoding: the SOP Instance UID must not change
            # (pydicom 3 generates a new one unless told otherwise).
            ds.decompress(generate_instance_uid=False)
            log.info("transcoded %s from %s for a receiver that does not accept it",
                     ds.SOPInstanceUID, ts)
        except Exception:  # noqa: BLE001
            log.exception("could not decompress %s (%s)", ds.SOPInstanceUID, ts)
    return ds


def _accepted_map(contexts) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for cx in contexts:
        if cx.transfer_syntax:
            out.setdefault(str(cx.abstract_syntax), set()).add(str(cx.transfer_syntax[0]))
    return out


def _probe(dest: dict, contexts) -> dict[str, set[str]]:
    """Which (SOP class, transfer syntax) pairs the move destination accepts,
    learned by negotiating the same contexts once before the C-MOVE."""
    from pynetdicom import AE
    ae = AE(ae_title=config.ae_title())
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = config.network_timeout()
    for cx in contexts:
        ae.add_requested_context(cx.abstract_syntax, cx.transfer_syntax)
    assoc = ae.associate(dest["host"], int(dest["port"]), ae_title=dest["ae_title"],
                         tls_args=_tls_args(dest))
    if not assoc.is_established:
        return {}
    try:
        return _accepted_map(assoc.accepted_contexts)
    finally:
        assoc.release()


def _tls_args(node: dict):
    ctx = nodes.tls_context(node)
    return (ctx, None) if ctx else None


def _contexts_for(instances: list[dict]):
    from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
    from pynetdicom import build_context
    pairs: dict[str, set[str]] = {}
    for i in instances:
        ts = i.get("transfer_syntax") or ExplicitVRLittleEndian
        pairs.setdefault(i["sop_class_uid"], set()).add(ts)
    ctxs = []
    for sop_class, tss in list(pairs.items())[:128]:
        syntaxes = list(dict.fromkeys([*sorted(tss), ExplicitVRLittleEndian,
                                       ImplicitVRLittleEndian]))
        ctxs.append(build_context(sop_class, syntaxes))
    return ctxs


def handle_move(event):
    node, ok = _node_for(event, "allow_retrieve")
    if not ok:
        yield NOT_AUTHORIZED, None
        return
    dest_ae = event.move_destination
    dest_ae = dest_ae.decode() if isinstance(dest_ae, bytes) else str(dest_ae)
    dest = nodes.by_ae(dest_ae.strip())
    if not dest or dest.get("kind") != "dimse" or not dest.get("is_move_destination"):
        log.warning("C-MOVE to unknown/unauthorised destination %r refused", dest_ae)
        yield None, None
        return
    instances = _consented(node, _matching_instances(event.identifier))
    contexts = _contexts_for(instances) or None
    accepted = _probe(dest, contexts) if contexts else {}
    kwargs: dict[str, Any] = {"ae_title": dest["ae_title"], "contexts": contexts}
    if _tls_args(dest):
        kwargs["tls_args"] = _tls_args(dest)
    yield dest["host"], int(dest["port"]), kwargs
    yield len(instances)
    for ds in _datasets(instances, accepted):
        if event.is_cancelled:
            yield CANCEL, None
            return
        yield PENDING, ds
    messages.log(direction="out", protocol="dicom", message_type="C-MOVE",
                 peer=dest_ae, status="ok", payload=f"{len(instances)} instance(s) to {dest_ae}")


def handle_get(event):
    node, ok = _node_for(event, "allow_retrieve")
    if not ok:
        yield NOT_AUTHORIZED, None
        return
    instances = _consented(node, _matching_instances(event.identifier))
    yield len(instances)
    # C-GET sends on this same association: transcode to what it accepted.
    accepted = _accepted_map(event.assoc.accepted_contexts)
    for ds in _datasets(instances, accepted):
        if event.is_cancelled:
            yield CANCEL, None
            return
        yield PENDING, ds


def handle_n_create(event):
    from pynetdicom.sop_class import ModalityPerformedProcedureStep
    if event.request.AffectedSOPClassUID != ModalityPerformedProcedureStep:
        return PROCESSING_FAILURE, None
    node, ok = _node_for(event, "allow_store")
    if not ok:
        return NOT_AUTHORIZED, None
    req = event.request
    sop_uid = req.AffectedSOPInstanceUID or worklist.generate_uid()
    attrs = event.attribute_list
    if worklist.mpps_get(str(sop_uid)):
        return DUPLICATE_SOP_INSTANCE, None  # PS3.7 10.1.5.1.6
    try:
        worklist.mpps_create(str(sop_uid), attrs, station_ae=_calling(event))
    except Exception:  # noqa: BLE001
        log.exception("MPPS N-CREATE failed")
        return PROCESSING_FAILURE, None
    ds = Dataset()
    ds.update(attrs)
    ds.SOPClassUID = ModalityPerformedProcedureStep
    ds.SOPInstanceUID = sop_uid
    return SUCCESS, ds


def handle_n_set(event):
    node, ok = _node_for(event, "allow_store")
    if not ok:
        return NOT_AUTHORIZED, None
    sop_uid = str(event.request.RequestedSOPInstanceUID)
    try:
        out = worklist.mpps_set(sop_uid, event.modification_list)
    except ValueError:
        return 0xC310, None  # MPPS not in progress (PS3.4 F.7.2.2.4)
    if out is None:
        return NO_SUCH_OBJECT, None
    ds = Dataset()
    ds.update(event.modification_list)
    return SUCCESS, ds


def handle_n_action(event):
    """Storage Commitment request: acknowledge, then report asynchronously."""
    from pynetdicom.sop_class import StorageCommitmentPushModel
    if event.request.RequestedSOPClassUID != StorageCommitmentPushModel:
        return PROCESSING_FAILURE, None
    node, ok = _node_for(event, None)
    if not ok:
        return NOT_AUTHORIZED, None
    info = event.action_information
    refs = [(str(r.ReferencedSOPClassUID), str(r.ReferencedSOPInstanceUID))
            for r in info.get("ReferencedSOPSequence") or []]
    txn = str(info.get("TransactionUID") or "")
    calling = _calling(event)
    threading.Thread(target=_send_commitment_report, args=(calling, txn, refs),
                     daemon=True, name="pacs-stgcmt").start()
    return SUCCESS, None


def _send_commitment_report(calling_ae: str, txn: str, refs: list[tuple[str, str]]) -> None:
    from pynetdicom import AE, build_role
    from pynetdicom.sop_class import StorageCommitmentPushModel

    node = nodes.by_ae(calling_ae)
    if not node or node.get("kind") != "dimse":
        log.warning("storage commitment: no node registered for %s; report not sent", calling_ae)
        return
    present = {i["sop_uid"] for i in index.instance_paths(sop_uids=[r[1] for r in refs])} \
        if refs else set()
    ok_refs = [r for r in refs if r[1] in present]
    bad_refs = [r for r in refs if r[1] not in present]
    ds = Dataset()
    ds.TransactionUID = txn

    def items(pairs, failed=False):
        out = []
        for cls, inst in pairs:
            it = Dataset()
            it.ReferencedSOPClassUID = cls
            it.ReferencedSOPInstanceUID = inst
            if failed:
                it.FailureReason = NO_SUCH_OBJECT
            out.append(it)
        return out
    if ok_refs:
        ds.ReferencedSOPSequence = items(ok_refs)
    if bad_refs:
        ds.FailedSOPSequence = items(bad_refs, failed=True)
    ae = AE(ae_title=config.ae_title())
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = config.network_timeout()
    ae.add_requested_context(StorageCommitmentPushModel)
    role = build_role(StorageCommitmentPushModel, scp_role=True)
    assoc = ae.associate(node["host"], int(node["port"]), ae_title=node["ae_title"],
                         ext_neg=[role])
    if not assoc.is_established:
        log.warning("storage commitment: could not reach %s", calling_ae)
        return
    try:
        assoc.send_n_event_report(ds, 1 if not bad_refs else 2, StorageCommitmentPushModel,
                                  "1.2.840.10008.1.20.1.1")
    finally:
        assoc.release()


def _refresh_allowlist(event) -> None:
    """Before negotiation, load the current allow-list of calling AE titles."""
    if not config.require_known_peers():
        event.assoc.ae.require_calling_aet = []
        return
    titles = [n["ae_title"] for n in nodes.list_nodes(kind="dimse", active_only=True)
              if n.get("ae_title")]
    # An empty list means "accept anyone" to pynetdicom, which is the opposite
    # of what an empty allow-list should mean.
    event.assoc.ae.require_calling_aet = titles or ["NO-PEERS-ALLOWED"]


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------
def _register_private_storage(uid: str) -> None:
    """Tell pynetdicom a vendor-private UID is a Storage SOP class."""
    from pynetdicom.service_class import StorageServiceClass
    from pynetdicom.sop_class import register_uid, uid_to_service_class
    try:
        if uid_to_service_class(uid) is StorageServiceClass:
            return
    except Exception:  # noqa: BLE001
        pass
    keyword = "PrivateStorage_" + uid.replace(".", "_")
    try:
        register_uid(uid, keyword, StorageServiceClass)
    except ValueError:
        log.debug("private SOP class %s already registered", uid)


class DimseServer:
    def __init__(self, *, ae_title: Optional[str] = None, bind: Optional[str] = None,
                 port: Optional[int] = None) -> None:
        self.ae_title = ae_title or config.ae_title()
        self.bind = bind or config.dimse_bind()
        self.port = port if port is not None else config.dimse_port()
        self._server = None

    def _ae(self):
        from pynetdicom import (AE, ALL_TRANSFER_SYNTAXES, AllStoragePresentationContexts,
                                evt)
        from pynetdicom.sop_class import (ModalityPerformedProcedureStep,
                                          ModalityWorklistInformationFind,
                                          PatientRootQueryRetrieveInformationModelFind,
                                          PatientRootQueryRetrieveInformationModelGet,
                                          PatientRootQueryRetrieveInformationModelMove,
                                          StorageCommitmentPushModel,
                                          StudyRootQueryRetrieveInformationModelFind,
                                          StudyRootQueryRetrieveInformationModelGet,
                                          StudyRootQueryRetrieveInformationModelMove,
                                          Verification)
        ae = AE(ae_title=self.ae_title)
        t = config.network_timeout()
        ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = t
        ae.maximum_associations = settings.env_int("PACS_MAX_ASSOCIATIONS", 32)
        ae.require_called_aet = config.require_called_ae()
        ae.add_supported_context(Verification)
        for cx in AllStoragePresentationContexts:
            # scp_role=True lets a C-GET requestor act as Storage SCP on this
            # association; scu_role keeps ordinary C-STORE working.
            ae.add_supported_context(cx.abstract_syntax, ALL_TRANSFER_SYNTAXES,
                                     scu_role=True, scp_role=True)
        for sop in (PatientRootQueryRetrieveInformationModelFind,
                    StudyRootQueryRetrieveInformationModelFind,
                    PatientRootQueryRetrieveInformationModelMove,
                    StudyRootQueryRetrieveInformationModelMove,
                    PatientRootQueryRetrieveInformationModelGet,
                    StudyRootQueryRetrieveInformationModelGet,
                    ModalityWorklistInformationFind, ModalityPerformedProcedureStep):
            ae.add_supported_context(sop)
        ae.add_supported_context(StorageCommitmentPushModel, scu_role=True, scp_role=True)
        # Vendor-private storage SOP classes (e.g. scanner raw data) that some
        # devices push alongside the images.
        for uid in config.extra_sop_classes():
            _register_private_storage(uid)
            ae.add_supported_context(uid, ALL_TRANSFER_SYNTAXES, scu_role=True, scp_role=True)
        if config.accept_any_storage():
            from pynetdicom import _config as pyn_config
            pyn_config.UNRESTRICTED_STORAGE_SERVICE = True
        handlers = [
            (evt.EVT_CONN_OPEN, _refresh_allowlist),
            (evt.EVT_C_ECHO, handle_echo),
            (evt.EVT_C_STORE, handle_store),
            (evt.EVT_C_FIND, handle_find),
            (evt.EVT_C_MOVE, handle_move),
            (evt.EVT_C_GET, handle_get),
            (evt.EVT_N_CREATE, handle_n_create),
            (evt.EVT_N_SET, handle_n_set),
            (evt.EVT_N_ACTION, handle_n_action),
        ]
        return ae, handlers

    def _ssl_context(self):
        cert = settings.env("PACS_DIMSE_TLS_CERT", "")
        key = settings.env("PACS_DIMSE_TLS_KEY", "")
        if not (cert and key):
            return None
        import ssl
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(cert, key)
        ca = settings.env("PACS_DIMSE_TLS_CA", "")
        if ca:
            ctx.load_verify_locations(ca)
            ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    def start(self) -> "DimseServer":
        ae, handlers = self._ae()
        self._server = ae.start_server((self.bind, self.port), block=False,
                                       evt_handlers=handlers, ssl_context=self._ssl_context())
        self.port = self._server.server_address[1]
        log.info("DICOM SCP %s listening on %s:%d", self.ae_title, self.bind, self.port)
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    @property
    def running(self) -> bool:
        return self._server is not None


_server: Optional[DimseServer] = None


def start_from_config() -> Optional[DimseServer]:
    global _server
    if not config.dimse_enabled() or _server is not None:
        return _server
    _server = DimseServer().start()
    return _server


def stop() -> None:
    global _server
    if _server is not None:
        _server.stop()
        _server = None


def status() -> dict[str, Any]:
    return {"enabled": config.dimse_enabled(), "running": bool(_server and _server.running),
            "ae_title": config.ae_title(),
            "port": _server.port if _server else config.dimse_port(),
            "require_known_peers": config.require_known_peers()}
