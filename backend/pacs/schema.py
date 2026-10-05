"""PACS index tables: studies / series / instances, nodes, worklist, MPPS, jobs.

The DICOM files themselves live in :mod:`pacs.storage`; these tables are the
queryable index over them (what QIDO-RS and C-FIND search) plus the workflow
state (worklist, performed procedure steps, routing jobs, linked reports).

Study, series and instance rows are keyed by their DICOM UIDs, which are
globally unique by construction — so a study imported from another hospital
lands under the same key it had there, and a second import is a no-op.
"""
from __future__ import annotations

from clinicaldb import migrate

MODULE = "pacs"

V1 = """
CREATE TABLE IF NOT EXISTS pacs_studies (
  study_uid            TEXT PRIMARY KEY,
  person_id            TEXT,
  facility_oid         TEXT,
  patient_id           TEXT,
  issuer               TEXT,
  patient_name         TEXT,
  patient_name_norm    TEXT,
  patient_birth_date   TEXT,
  patient_sex          TEXT,
  accession            TEXT,
  study_id             TEXT,
  study_date           TEXT,
  study_time           TEXT,
  description          TEXT,
  referring_physician  TEXT,
  modalities           TEXT,
  body_parts           TEXT,
  num_series           INTEGER NOT NULL DEFAULT 0,
  num_instances        INTEGER NOT NULL DEFAULT 0,
  size_bytes           BIGINT NOT NULL DEFAULT 0,
  status               TEXT NOT NULL DEFAULT 'received',
  priority             TEXT,
  source               TEXT,
  origin_facility      TEXT,
  created_at           DOUBLE PRECISION NOT NULL,
  updated_at           DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pacs_studies_person ON pacs_studies(person_id);
CREATE INDEX IF NOT EXISTS ix_pacs_studies_pid ON pacs_studies(patient_id);
CREATE INDEX IF NOT EXISTS ix_pacs_studies_acc ON pacs_studies(accession);
CREATE INDEX IF NOT EXISTS ix_pacs_studies_date ON pacs_studies(study_date);

CREATE TABLE IF NOT EXISTS pacs_series (
  series_uid      TEXT PRIMARY KEY,
  study_uid       TEXT NOT NULL REFERENCES pacs_studies(study_uid) ON DELETE CASCADE,
  modality        TEXT,
  series_number   INTEGER,
  description     TEXT,
  body_part       TEXT,
  laterality      TEXT,
  station_name    TEXT,
  num_instances   INTEGER NOT NULL DEFAULT 0,
  created_at      DOUBLE PRECISION NOT NULL,
  updated_at      DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pacs_series_study ON pacs_series(study_uid);

CREATE TABLE IF NOT EXISTS pacs_instances (
  sop_uid          TEXT PRIMARY KEY,
  series_uid       TEXT NOT NULL REFERENCES pacs_series(series_uid) ON DELETE CASCADE,
  study_uid        TEXT NOT NULL,
  sop_class_uid    TEXT,
  instance_number  INTEGER,
  transfer_syntax  TEXT,
  path             TEXT NOT NULL,
  sha256           TEXT NOT NULL,
  size_bytes       BIGINT NOT NULL,
  rows_            INTEGER,
  columns_         INTEGER,
  frames           INTEGER,
  bits_allocated   INTEGER,
  photometric      TEXT,
  window_center    TEXT,
  window_width     TEXT,
  created_at       DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pacs_instances_series ON pacs_instances(series_uid);
CREATE INDEX IF NOT EXISTS ix_pacs_instances_study ON pacs_instances(study_uid);

CREATE TABLE IF NOT EXISTS pacs_nodes (
  id                   TEXT PRIMARY KEY,
  name                 TEXT NOT NULL,
  kind                 TEXT NOT NULL DEFAULT 'dimse',
  ae_title             TEXT,
  host                 TEXT,
  port                 INTEGER,
  base_url             TEXT,
  auth_env             TEXT,
  facility_oid         TEXT,
  issuer               TEXT,
  allow_store          INTEGER NOT NULL DEFAULT 1,
  allow_query          INTEGER NOT NULL DEFAULT 1,
  allow_retrieve       INTEGER NOT NULL DEFAULT 1,
  is_move_destination  INTEGER NOT NULL DEFAULT 1,
  active               INTEGER NOT NULL DEFAULT 1,
  last_echo_at         DOUBLE PRECISION,
  last_echo_ok         INTEGER,
  created_at           DOUBLE PRECISION NOT NULL,
  updated_at           DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pacs_nodes_ae ON pacs_nodes(ae_title);

CREATE TABLE IF NOT EXISTS pacs_worklist (
  id                      TEXT PRIMARY KEY,
  accession               TEXT NOT NULL UNIQUE,
  person_id               TEXT,
  patient_id              TEXT,
  issuer                  TEXT,
  patient_name            TEXT,
  patient_birth_date      TEXT,
  patient_sex             TEXT,
  modality                TEXT,
  station_ae              TEXT,
  station_name            TEXT,
  scheduled_start         TEXT,
  procedure_code          TEXT,
  procedure_description   TEXT,
  requested_procedure_id  TEXT,
  sps_id                  TEXT,
  referring_physician     TEXT,
  reason                  TEXT,
  priority                TEXT,
  status                  TEXT NOT NULL DEFAULT 'scheduled',
  study_uid               TEXT,
  order_id                TEXT,
  source                  TEXT,
  created_at              DOUBLE PRECISION NOT NULL,
  updated_at              DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pacs_wl_status ON pacs_worklist(status, scheduled_start);
CREATE INDEX IF NOT EXISTS ix_pacs_wl_person ON pacs_worklist(person_id);

CREATE TABLE IF NOT EXISTS pacs_mpps (
  sop_uid       TEXT PRIMARY KEY,
  worklist_id   TEXT,
  accession     TEXT,
  study_uid     TEXT,
  status        TEXT NOT NULL,
  modality      TEXT,
  station_ae    TEXT,
  started_at    TEXT,
  ended_at      TEXT,
  data          TEXT,
  created_at    DOUBLE PRECISION NOT NULL,
  updated_at    DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS pacs_jobs (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'queued',
  target        TEXT,
  study_uid     TEXT,
  params        TEXT,
  attempts      INTEGER NOT NULL DEFAULT 0,
  max_attempts  INTEGER NOT NULL DEFAULT 3,
  last_error    TEXT,
  result        TEXT,
  created_by    TEXT,
  created_at    DOUBLE PRECISION NOT NULL,
  updated_at    DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pacs_jobs_status ON pacs_jobs(status, created_at);

CREATE TABLE IF NOT EXISTS pacs_study_reports (
  id            TEXT PRIMARY KEY,
  study_uid     TEXT NOT NULL,
  person_id     TEXT,
  status        TEXT NOT NULL DEFAULT 'draft',
  source        TEXT NOT NULL DEFAULT 'radiologist',
  author        TEXT,
  text          TEXT,
  data          TEXT,
  created_at    DOUBLE PRECISION NOT NULL,
  updated_at    DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pacs_reports_study ON pacs_study_reports(study_uid)
"""



def _v2(conn) -> None:
    # Outbound participation in federated search (distinct from the inbound
    # allow_query permission, which modalities need for worklist queries).
    migrate.add_column(conn, "pacs_nodes", "federate", "INTEGER NOT NULL DEFAULT 0")


def _v3(conn) -> None:
    # Per-node DICOM TLS / mTLS (certificate paths come from env vars, never
    # the DB), preferred retrieve method and character set for older devices.
    for col, decl in (("tls", "INTEGER NOT NULL DEFAULT 0"), ("tls_ca_env", "TEXT"),
                      ("tls_cert_env", "TEXT"), ("tls_key_env", "TEXT"),
                      ("prefer_cget", "INTEGER NOT NULL DEFAULT 0"), ("charset", "TEXT")):
        migrate.add_column(conn, "pacs_nodes", col, decl)
    # Scheduled times are stored in DICOM form (YYYYMMDDHHMMSS) so MWL date
    # matching works for entries created from HL7/FHIR orders (ISO format).
    from pacs.worklist import dicom_dt
    for r in conn.execute("SELECT id, scheduled_start FROM pacs_worklist "
                          "WHERE scheduled_start IS NOT NULL").fetchall():
        fixed = dicom_dt(r["scheduled_start"])
        if fixed != r["scheduled_start"]:
            conn.execute("UPDATE pacs_worklist SET scheduled_start=? WHERE id=?", (fixed, r["id"]))


migrate.register(MODULE, [(1, V1), (2, _v2), (3, _v3)])
