"""PACS settings, all from the environment (documented in .env.example)."""
from __future__ import annotations

from pathlib import Path

from clinicaldb import settings as s


def storage_dir() -> Path:
    p = s.env("PACS_STORAGE_DIR", "")
    return Path(p) if p else s.data_dir() / "pacs"


def ae_title() -> str:
    return s.env("PACS_AE_TITLE", "ARANMED")[:16]


def dimse_enabled() -> bool:
    return s.env_bool("PACS_DIMSE_ENABLED", False)


def dimse_bind() -> str:
    return s.env("PACS_DIMSE_BIND", "0.0.0.0")


def dimse_port() -> int:
    return s.env_int("PACS_DIMSE_PORT", 11112)


def require_known_peers() -> bool:
    """Reject associations from AEs not registered in pacs_nodes."""
    return s.env_bool("PACS_REQUIRE_KNOWN_PEERS", True)


def dicomweb_prefix() -> str:
    return "/" + s.env("DICOMWEB_PREFIX", "/api/dicom-web").strip("/")


def duplicate_policy() -> str:
    """keep (default, idempotent) | overwrite | reject — for a re-sent SOP UID."""
    v = s.env("PACS_DUPLICATE_POLICY", "keep").lower()
    return v if v in ("keep", "overwrite", "reject") else "keep"


def default_issuer() -> str:
    """Issuer assumed for a PatientID that arrives without IssuerOfPatientID."""
    return s.env("PACS_DEFAULT_ISSUER", "") or s.facility_oid()


def max_upload_mb() -> int:
    return s.env_int("PACS_MAX_UPLOAD_MB", 2048)


def network_timeout() -> float:
    return s.env_float("PACS_NETWORK_TIMEOUT_SEC", 30.0)


def job_max_attempts() -> int:
    return s.env_int("PACS_JOB_MAX_ATTEMPTS", 3)


def http_timeout() -> float:
    return s.env_float("PACS_HTTP_TIMEOUT_SEC", 60.0)


def extra_sop_classes() -> list[str]:
    """Vendor-private storage SOP class UIDs to accept (comma-separated)."""
    return [u.strip() for u in s.env("PACS_EXTRA_SOP_CLASSES", "").split(",") if u.strip()]


def accept_any_storage() -> bool:
    """Accept C-STORE for any SOP class a device proposes, known or not."""
    return s.env_bool("PACS_ACCEPT_ANY_STORAGE", False)


def require_called_ae() -> bool:
    """Reject associations addressed to an AE title other than ours."""
    return s.env_bool("PACS_REQUIRE_CALLED_AE", True)


def default_charset() -> str:
    """Character set assumed for text in objects that do not declare one
    (older local modalities): tried after UTF-8 when decoding fails."""
    return s.env("PACS_FALLBACK_CHARSET", "cp1256")
