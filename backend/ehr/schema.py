"""Relational EHR tables (FHIR-aligned), on the shared MPI spine.

Every clinical row carries:

* ``person_id``      — the MPI enterprise id (never a local MRN),
* ``facility_oid``   — the facility that holds this copy,
* ``source_facility`` / ``source_id`` — where the record was *authored* and
  its id there; ``UNIQUE(source_facility, source_id)`` makes importing the
  same record twice (transfer package re-sent, federated fetch repeated) a
  no-op instead of a duplicate,
* ``version`` / ``deleted`` — optimistic versioning and soft delete, which
  the FHIR server exposes as ``meta.versionId`` and ``_history``.

Codes are stored as (system, code, display) plus the original text — the
same "never guess a code" rule as backend/fhir.py.
"""
from __future__ import annotations

from clinicaldb import migrate

MODULE = "ehr"

_COMMON = """
  id               TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL,
  facility_oid     TEXT NOT NULL,
  source_facility  TEXT,
  source_id        TEXT,
  encounter_id     TEXT,
  status           TEXT,
  code_system      TEXT,
  code             TEXT,
  display          TEXT,
  text             TEXT,
  note             TEXT,
  data             TEXT,
  version          INTEGER NOT NULL DEFAULT 1,
  deleted          INTEGER NOT NULL DEFAULT 0,
  created_by       TEXT,
  created_at       DOUBLE PRECISION NOT NULL,
  updated_at       DOUBLE PRECISION NOT NULL
"""


def _table(name: str, extra: str) -> str:
    return (f"CREATE TABLE IF NOT EXISTS {name} ({_COMMON},\n{extra},\n"
            f"  UNIQUE (source_facility, source_id)\n);\n"
            f"CREATE INDEX IF NOT EXISTS ix_{name}_person ON {name}(person_id, deleted);\n")


V1 = "".join([
    _table("ehr_encounters", """
  class            TEXT,
  type_text        TEXT,
  reason           TEXT,
  start_at         TEXT,
  end_at           TEXT,
  location         TEXT,
  department       TEXT,
  attending        TEXT,
  admit_source     TEXT,
  disposition      TEXT,
  priority         TEXT"""),
    _table("ehr_conditions", """
  clinical_status  TEXT,
  verification     TEXT,
  category         TEXT,
  severity         TEXT,
  onset            TEXT,
  abatement        TEXT"""),
    _table("ehr_allergies", """
  reaction         TEXT,
  severity         TEXT,
  criticality      TEXT,
  category         TEXT,
  onset            TEXT"""),
    _table("ehr_medications", """
  kind             TEXT NOT NULL DEFAULT 'statement',
  dose             TEXT,
  route            TEXT,
  frequency        TEXT,
  frequency_hours  DOUBLE PRECISION,
  start_at         TEXT,
  end_at           TEXT,
  prescriber       TEXT,
  indication       TEXT"""),
    _table("ehr_observations", """
  category         TEXT,
  value_num        DOUBLE PRECISION,
  value_text       TEXT,
  unit             TEXT,
  ref_low          DOUBLE PRECISION,
  ref_high         DOUBLE PRECISION,
  interpretation   TEXT,
  effective        TEXT,
  performer        TEXT,
  panel_id         TEXT"""),
    _table("ehr_procedures", """
  performed        TEXT,
  performer        TEXT,
  body_site        TEXT,
  outcome          TEXT"""),
    _table("ehr_immunizations", """
  occurrence       TEXT,
  lot              TEXT,
  dose_number      TEXT,
  site             TEXT,
  route            TEXT,
  performer        TEXT"""),
    _table("ehr_documents", """
  doc_type         TEXT,
  title            TEXT,
  author           TEXT,
  content_type     TEXT,
  content          TEXT,
  size_bytes       BIGINT,
  sha256           TEXT,
  effective        TEXT"""),
    _table("ehr_service_requests", """
  category         TEXT,
  intent           TEXT,
  priority         TEXT,
  reason           TEXT,
  requester        TEXT,
  performer        TEXT,
  accession        TEXT,
  occurrence       TEXT"""),
    _table("ehr_diagnostic_reports", """
  category         TEXT,
  effective        TEXT,
  issued           TEXT,
  conclusion       TEXT,
  study_uid        TEXT,
  performer        TEXT,
  result_ids       TEXT"""),
    _table("ehr_consents", """
  scope            TEXT,
  category         TEXT,
  grantee          TEXT,
  purposes         TEXT,
  start_at         TEXT,
  end_at           TEXT"""),
    """
CREATE TABLE IF NOT EXISTS ehr_practitioners (
  id            TEXT PRIMARY KEY,
  facility_oid  TEXT NOT NULL,
  identifier    TEXT,
  name          TEXT NOT NULL,
  role          TEXT,
  specialty     TEXT,
  phone         TEXT,
  email         TEXT,
  user_id       BIGINT,
  active        INTEGER NOT NULL DEFAULT 1,
  created_at    DOUBLE PRECISION NOT NULL,
  updated_at    DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS ehr_breakglass (
  id          TEXT PRIMARY KEY,
  person_id   TEXT NOT NULL,
  user_id     BIGINT,
  username    TEXT,
  reason      TEXT NOT NULL,
  expires_at  DOUBLE PRECISION NOT NULL,
  created_at  DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_breakglass_person ON ehr_breakglass(person_id, user_id);

CREATE TABLE IF NOT EXISTS ehr_history (
  id            TEXT PRIMARY KEY,
  resource      TEXT NOT NULL,
  resource_id   TEXT NOT NULL,
  version       INTEGER NOT NULL,
  snapshot      TEXT NOT NULL,
  changed_by    TEXT,
  changed_at    DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ehr_history_res ON ehr_history(resource, resource_id);

CREATE TABLE IF NOT EXISTS ehr_transfers (
  id                  TEXT PRIMARY KEY,
  person_id           TEXT NOT NULL,
  direction           TEXT NOT NULL,
  from_facility       TEXT NOT NULL,
  to_facility         TEXT NOT NULL,
  remote_id           TEXT,
  status              TEXT NOT NULL,
  urgency             TEXT,
  reason              TEXT,
  clinical_summary    TEXT,
  transport_mode      TEXT,
  ems_unit            TEXT,
  requested_by        TEXT,
  accepted_by         TEXT,
  rejection_reason    TEXT,
  eta                 TEXT,
  package_status      TEXT,
  package_manifest    TEXT,
  history             TEXT,
  created_at          DOUBLE PRECISION NOT NULL,
  updated_at          DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_transfers_person ON ehr_transfers(person_id);
CREATE INDEX IF NOT EXISTS ix_transfers_status ON ehr_transfers(status, updated_at);

CREATE TABLE IF NOT EXISTS ems_notifications (
  id               TEXT PRIMARY KEY,
  person_id        TEXT,
  encounter_id     TEXT,
  source_facility  TEXT,
  source_id        TEXT,
  unit             TEXT,
  eta              TEXT,
  status           TEXT NOT NULL,
  triage           TEXT,
  chief_complaint  TEXT,
  summary          TEXT,
  data             TEXT,
  acknowledged_by  TEXT,
  created_at       DOUBLE PRECISION NOT NULL,
  updated_at       DOUBLE PRECISION NOT NULL,
  UNIQUE (source_facility, source_id)
);
CREATE INDEX IF NOT EXISTS ix_ems_status ON ems_notifications(status, updated_at)
""",
])

migrate.register(MODULE, [(1, V1)])
