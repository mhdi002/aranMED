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


def max_pdu() -> int:
    """Largest PDU we accept (0 = unlimited). pynetdicom's 16 KB default
    makes large studies slow; peers' own limits are always honoured."""
    return max(0, s.env_int("PACS_MAX_PDU", 0))


def store_chunked() -> bool:
    """Write incoming C-STORE data sets straight to disk instead of memory,
    so multi-hundred-MB enhanced/tomosynthesis objects don't exhaust RAM."""
    return s.env_bool("PACS_STORE_CHUNKED", True)


def ae_case_sensitive() -> bool:
    """AE titles are case-sensitive in DICOM; some sites configure devices
    inconsistently. False matches calling/move-destination AEs ignoring case."""
    return s.env_bool("PACS_AE_CASE_SENSITIVE", True)


def dimse_tls_port() -> int:
    """Separate DICOM TLS listener (PS3.15 B.12 recommends 2762); 0 = off,
    in which case a configured certificate turns the main port into TLS."""
    return s.env_int("PACS_DIMSE_TLS_PORT", 0)


def stgcmt_same_assoc_sec() -> float:
    """How long the requesting association may stay open for the Storage
    Commitment report to be sent on it (nodes with stgcmt_mode same/auto)."""
    return s.env_float("PACS_STGCMT_SAME_ASSOC_SEC", 2.0)


def stgcmt_max_attempts() -> int:
    return s.env_int("PACS_STGCMT_MAX_ATTEMPTS", 10)


def node_max_pdu(node: dict | None) -> int:
    """Max PDU we advertise when we open an association to *node*: its own
    setting (some old devices mishandle 'unlimited'), else PACS_MAX_PDU."""
    v = (node or {}).get("max_pdu")
    try:
        return max(0, int(v)) if v not in (None, "") else max_pdu()
    except (TypeError, ValueError):
        return max_pdu()
