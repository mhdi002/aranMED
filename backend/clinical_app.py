"""Wires the clinical packages (MPI, PACS, EHR, interop) into the FastAPI app.

``app.py`` calls :func:`mount` once; everything else — routers, start-up
seeding, network listeners (DICOM, MLLP) — is owned here so the core app
module only grows by two lines. Packages are mounted in dependency order;
each router guards itself with ``rbac.require`` like the rest of the API.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

log = logging.getLogger("clinical_app")

_started: list = []


def mount(app: FastAPI) -> None:
    from clinicaldb import api as clinicaldb_api
    from pacs import api as pacs_api
    from pacs import config as pacs_config
    from pacs import dicomweb, worklist  # noqa: F401  (worklist registers ingest hook)

    from ehr import api as ehr_api, bridge as ehr_bridge, links as ehr_links

    app.include_router(clinicaldb_api.router)
    app.include_router(pacs_api.router)
    app.include_router(ehr_api.router)
    ehr_bridge.install()
    ehr_links.install()

    from interop import api as interop_api, federation, fhir_server
    app.include_router(interop_api.router)
    app.include_router(fhir_server.make_router())
    federation.install()
    clinicaldb_api.announce("fhir", version="4.0.1", base=fhir_server.prefix(),
                            operations=["$everything", "$summary", "$match", "$ihe-pix"],
                            interactions=["read", "vread", "search", "create", "update", "delete",
                                          "history", "transaction", "batch"])
    clinicaldb_api.announce("hl7v2", transport=["MLLP", "HTTP"],
                            messages=["ADT^A01-A40", "ORM^O01", "OMI^O23", "ORU^R01", "MDM^T02",
                                      "VXU^V04", "QBP^Q22", "QBP^Q23"])
    clinicaldb_api.announce("cda", documents=["C-CDA R2.1 CCD"], directions=["export", "import"])
    clinicaldb_api.announce("ems", formats=["NEMSIS v3.5 XML", "NEMSIS JSON", "FHIR Bundle"])
    clinicaldb_api.announce("transfer", version="1",
                            endpoints=["/api/transfers/inbound", "/api/transfers/inbound/{id}/status",
                                       "/api/transfers/inbound/{id}/package"])
    app.include_router(dicomweb.make_router())
    clinicaldb_api.announce("dicomweb", base=pacs_config.dicomweb_prefix(),
                            services=["QIDO-RS", "WADO-RS", "STOW-RS", "WADO-URI"])
    clinicaldb_api.announce("dicom", ae_title=pacs_config.ae_title(),
                            services=["C-ECHO", "C-STORE", "C-FIND", "C-MOVE", "C-GET",
                                      "MWL", "MPPS", "StorageCommitment"])

    async def _startup() -> None:
        from clinicaldb import facilities
        from pacs import dimse, jobs
        loc = facilities.seed_local()
        n = facilities.seed_peers_from_file()
        log.info("facility %s (%s) ready; %d peer(s) seeded", loc["name"], loc["oid"], n)
        for stop in start_listeners():
            async def _stop(fn=stop) -> None:
                fn()
            _started.append(_stop)
        jobs.requeue_stale()

        async def _stop_jobs() -> None:
            jobs.shutdown()
        _started.append(_stop_jobs)

    async def _shutdown() -> None:
        for stop in reversed(_started):
            try:
                await stop()
            except Exception:  # noqa: BLE001
                log.exception("clinical shutdown step failed")
        _started.clear()

    app.router.on_startup.append(_startup)
    app.router.on_shutdown.append(_shutdown)


def start_listeners() -> list:
    """Start the enabled network listeners; return their stop callables."""
    from interop import mllp
    from pacs import dimse
    stops = []
    if dimse.start_from_config():
        stops.append(dimse.stop)
    if mllp.start_from_config():
        stops.append(mllp.stop)
    return stops
