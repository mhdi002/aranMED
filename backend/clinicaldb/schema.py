"""Facility registry and Master Patient Index (MPI) tables.

These are the spine the PACS and EHR tables hang off: every study, encounter
or observation carries ``person_id`` (this MPI's enterprise id) plus the
``facility_oid`` that produced it, so a record moved or read between
hospitals keeps both "who is this" and "where did it come from".

See migrate.py for the portable-SQL rules every statement here follows.
"""
from __future__ import annotations

from clinicaldb import migrate

MODULE = "mpi"

V1 = """
CREATE TABLE IF NOT EXISTS facilities (
  id             TEXT PRIMARY KEY,
  oid            TEXT NOT NULL UNIQUE,
  name           TEXT NOT NULL,
  kind           TEXT NOT NULL DEFAULT 'hospital',
  is_local       INTEGER NOT NULL DEFAULT 0,
  ae_title       TEXT,
  dicom_host     TEXT,
  dicom_port     INTEGER,
  base_url       TEXT,
  fhir_base      TEXT,
  dicomweb_base  TEXT,
  mllp_host      TEXT,
  mllp_port      INTEGER,
  auth_mode      TEXT NOT NULL DEFAULT 'jwt',
  secret_env     TEXT,
  trust_level    TEXT NOT NULL DEFAULT 'peer',
  active         INTEGER NOT NULL DEFAULT 1,
  meta           TEXT,
  created_at     DOUBLE PRECISION NOT NULL,
  updated_at     DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS mpi_persons (
  id               TEXT PRIMARY KEY,
  family_norm      TEXT,
  given_norm       TEXT,
  birth_date       TEXT,
  sex              TEXT,
  demographics     TEXT NOT NULL,
  home_facility    TEXT,
  status           TEXT NOT NULL DEFAULT 'active',
  merged_into      TEXT,
  created_at       DOUBLE PRECISION NOT NULL,
  updated_at       DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_mpi_family ON mpi_persons(family_norm);
CREATE INDEX IF NOT EXISTS ix_mpi_birth ON mpi_persons(birth_date);

CREATE TABLE IF NOT EXISTS patient_identifiers (
  id            TEXT PRIMARY KEY,
  person_id     TEXT NOT NULL REFERENCES mpi_persons(id) ON DELETE CASCADE,
  system        TEXT NOT NULL,
  value         TEXT NOT NULL,
  type          TEXT NOT NULL DEFAULT 'MR',
  facility_oid  TEXT,
  created_at    DOUBLE PRECISION NOT NULL,
  UNIQUE (system, value)
);
CREATE INDEX IF NOT EXISTS ix_pid_person ON patient_identifiers(person_id);

CREATE TABLE IF NOT EXISTS mpi_links (
  id            TEXT PRIMARY KEY,
  person_id     TEXT NOT NULL,
  other_id      TEXT NOT NULL,
  kind          TEXT NOT NULL,
  score         DOUBLE PRECISION,
  status        TEXT NOT NULL DEFAULT 'pending',
  reason        TEXT,
  created_by    TEXT,
  created_at    DOUBLE PRECISION NOT NULL,
  resolved_at   DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS ix_mpi_links_person ON mpi_links(person_id);

CREATE TABLE IF NOT EXISTS interop_messages (
  id            TEXT PRIMARY KEY,
  direction     TEXT NOT NULL,
  protocol      TEXT NOT NULL,
  message_type  TEXT,
  peer          TEXT,
  control_id    TEXT,
  status        TEXT NOT NULL,
  person_id     TEXT,
  payload       TEXT,
  response      TEXT,
  error         TEXT,
  created_at    DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_interop_msgs_ts ON interop_messages(created_at);
CREATE INDEX IF NOT EXISTS ix_interop_msgs_proto ON interop_messages(protocol, created_at)
"""


def _v2(conn) -> None:
    # Normalised name tokens across the primary name *and* recorded aliases,
    # so a search in Latin script finds a person registered in Persian.
    migrate.add_column(conn, "mpi_persons", "name_tokens", "TEXT")


migrate.register(MODULE, [(1, V1), (2, _v2)])
