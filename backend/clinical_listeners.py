"""Standalone process for the clinical network listeners (DICOM SCP, HL7 MLLP).

The HTTP backend may run as several replicas behind the gateway; a DICOM or
MLLP port can only be bound once. Docker Compose therefore runs this module
as its own singleton service (``clinical-listeners``) sharing the database
and PACS storage with the API replicas:

    python -m clinical_listeners          # cwd = backend/

Each listener honours its own *_ENABLED flag, exactly as when embedded in
the API process.
"""
from __future__ import annotations

import logging
import signal
import threading

log = logging.getLogger("clinical_listeners")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    import clinical_app
    from clinicaldb import facilities

    facilities.seed_local()
    facilities.seed_peers_from_file()
    stops = clinical_app.start_listeners()
    if not stops:
        log.warning("no listener enabled (PACS_DIMSE_ENABLED / HL7_MLLP_ENABLED); exiting")
        return
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    done.wait()
    for stop in reversed(stops):
        stop()


if __name__ == "__main__":
    main()
