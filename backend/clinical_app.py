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

    app.include_router(clinicaldb_api.router)

    async def _startup() -> None:
        from clinicaldb import facilities
        loc = facilities.seed_local()
        n = facilities.seed_peers_from_file()
        log.info("facility %s (%s) ready; %d peer(s) seeded", loc["name"], loc["oid"], n)

    async def _shutdown() -> None:
        for stop in reversed(_started):
            try:
                await stop()
            except Exception:  # noqa: BLE001
                log.exception("clinical shutdown step failed")
        _started.clear()

    app.router.on_startup.append(_startup)
    app.router.on_shutdown.append(_shutdown)
