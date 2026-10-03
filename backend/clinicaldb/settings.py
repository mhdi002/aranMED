"""Environment-driven settings for the clinical packages (PACS, EHR, interop).

Read through functions rather than import-time constants so a test (or a
multi-tenant launcher) can change the environment and have the next call see
it. Every value has a documented key in ``.env.example`` and
``docs/core/CONFIGURATION.md``; nothing here names a real host or hospital.
"""
from __future__ import annotations

import os
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key, str(default)))
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(env(key, str(default)))
    except ValueError:
        return default


def env_bool(key: str, default: bool = False) -> bool:
    raw = env(key, "")
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


# --- Local facility identity ------------------------------------------------
# The OID is the facility's globally unique name: every MRN this hospital
# issues is namespaced under it, so identifiers from different hospitals can
# coexist in one MPI without colliding. The default is a clearly-local
# placeholder under the "2.25" UUID arc; a real deployment sets its own.
def facility_oid() -> str:
    return env("FACILITY_OID", "2.25.0.1")


def facility_name() -> str:
    return env("FACILITY_NAME", "AranMed Local Facility")


def facility_kind() -> str:
    return env("FACILITY_KIND", "hospital")


def public_base_url() -> str:
    """Externally reachable base URL of this backend (for peers/links)."""
    return env("PUBLIC_BASE_URL", "").rstrip("/")


# --- Identifier systems ------------------------------------------------------
def mrn_system(oid: str | None = None) -> str:
    """Identifier system URI for MRNs issued by facility *oid*."""
    return f"urn:oid:{oid or facility_oid()}"


def national_id_system() -> str:
    return env("NATIONAL_ID_SYSTEM", "urn:aranmed:national-id")


# --- MPI matching ------------------------------------------------------------
def mpi_auto_link_score() -> float:
    return env_float("MPI_AUTO_LINK_SCORE", 0.92)


def mpi_review_score() -> float:
    return env_float("MPI_REVIEW_SCORE", 0.75)


# --- Storage -----------------------------------------------------------------
def data_dir() -> Path:
    p = env("CLINICAL_DATA_DIR", "")
    return Path(p) if p else _BACKEND / "data"
