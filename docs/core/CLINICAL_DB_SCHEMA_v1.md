# Clinical Database Schema v1 (MPI · PACS · EHR · Interop)

Companion docs: [`PACS_v1.md`](PACS_v1.md) · [`EHR_INTEROP_v1.md`](EHR_INTEROP_v1.md) · [`CONFIGURATION.md`](CONFIGURATION.md)

## 1. Why one schema, the same everywhere

A patient moves between hospitals. Their records must be readable, importable and linkable at the next hospital without a translation layer per site. So every AranMed hospital runs the **same versioned schema**, on SQLite or Postgres, and all records are keyed by identifiers that are **globally unique without coordination**:

| Key | Used for | Why it is collision-free |
|---|---|---|
| UUID (`TEXT`) | MPI persons, EHR rows, jobs, transfers | random 128-bit |
| DICOM UIDs | studies / series / instances | globally unique by the DICOM standard |
| `(system, value)` | patient identifiers | `system` is a URI; MRNs are namespaced by the issuing facility's OID (`urn:oid:<FACILITY_OID>`) |
| `(source_facility, source_id)` | every EHR row | where the record was *authored* and its id there |

The `UNIQUE(source_facility, source_id)` constraint on every EHR table is what makes moving data idempotent: a transfer package or a re-imported CCD updates the rows it already created instead of duplicating them. Records keep their authoring facility after being copied, so the next hospital can still tell where they came from.

`GET /api/interop/capabilities` publishes the applied schema versions. Peers compare them before exchanging data, and `POST /api/interop/peers/test` checks the match.

## 2. Portability rules (`backend/clinicaldb/migrate.py`)

All schema for the clinical packages is written once, in SQL that both SQLite and Postgres accept:

- `TEXT` primary keys; never auto-increment integers.
- `DOUBLE PRECISION` epoch timestamps. SQLite gives them REAL affinity; Postgres uses float8. Plain `REAL` would be float4 on Postgres and lose seconds.
- `INTEGER` 0/1 booleans; JSON stored as `TEXT`.
- Idempotent DDL (`IF NOT EXISTS`). Column additions go through `migrate.add_column`.
- No `;` inside statements, because the Postgres facade splits scripts on it.

Each package registers `(version, sql | callable)` steps under its module name. Applied versions are recorded in `schema_migrations`. `db.register_schema_hook` re-applies them whenever the database is initialised. The suite runs against SQLite, Postgres, and a **mixed network** (one hospital on each).

## 3. Tables

### MPI and registry (`module = mpi`)

| Table | Purpose |
|---|---|
| `facilities` | This hospital (seeded from `FACILITY_*`) and its peers: OID, endpoints (FHIR, DICOMweb, DIMSE, MLLP), `trust_level`, `secret_env` (the name of the env var holding the shared secret, never the secret itself) |
| `mpi_persons` | One row per real person. `demographics` is encrypted (`phi_crypto`); normalised name tokens, birth date and sex stay in clear for candidate lookup. Merged persons point to `merged_into` |
| `patient_identifiers` | `(system, value)` unique; MRNs per facility, national id, legacy ids, EMS incident numbers |
| `mpi_links` | Possible duplicates awaiting review, applied merges |
| `interop_messages` | Every HL7 / FHIR / DICOM / transfer / EMS exchange, in and out |

### PACS (`module = pacs`)

`pacs_studies`, `pacs_series`, `pacs_instances` hold the index over DICOM files. Studies link to `person_id` and record `origin_facility`. The other tables are `pacs_nodes` (remote AEs and DICOMweb/Orthanc peers with permissions and `federate`), `pacs_worklist` (MWL), `pacs_mpps`, `pacs_jobs` (retrieve and send) and `pacs_study_reports`.

### EHR (`module = ehr`)

Every clinical table shares a common header:

```
id, person_id, facility_oid, source_facility, source_id, encounter_id, status,
code_system, code, display, text, note, data(JSON), version, deleted,
created_by, created_at, updated_at,  UNIQUE(source_facility, source_id)
```

The type-specific columns are:

| Table | Type-specific columns |
|---|---|
| `ehr_encounters` | class (AMB/EMER/IMP), type_text, reason, start_at, end_at, location, department, attending, admit_source, disposition, priority |
| `ehr_conditions` | clinical_status, verification, category, severity, onset, abatement |
| `ehr_allergies` | reaction, severity, criticality, category, onset |
| `ehr_medications` | kind (statement/request), dose, route, frequency, frequency_hours, start_at, end_at, prescriber, indication |
| `ehr_observations` | category (vital-signs/laboratory/…), value_num, value_text, unit, ref_low, ref_high, interpretation, effective, performer |
| `ehr_procedures` / `ehr_immunizations` | performed / occurrence, performer, body site / lot, dose number |
| `ehr_documents` | doc_type, title, author, content_type, **content (encrypted)**, sha256, effective |
| `ehr_service_requests` | category, intent, priority, reason, requester, accession (links to `pacs_worklist`) |
| `ehr_diagnostic_reports` | category (RAD/LAB), conclusion, study_uid (links to `pacs_studies`), result_ids |
| `ehr_consents` | category (`restricted` / `deny-sharing` / `permit-sharing`), grantee (OID or `*`), purposes, period |

The remaining EHR tables are:

- `ehr_history`: version snapshots, served as FHIR `_history`.
- `ehr_breakglass`: time-limited emergency grants.
- `ehr_transfers`: both sides of a transfer, linked by `remote_id`.
- `ems_notifications`: the ED pre-arrival board.

## 4. How the databases connect to each other

```
mpi_persons ─┬─ patient_identifiers        (who)
             ├─ pacs_studies ── series ── instances   (imaging, by person_id)
             ├─ ehr_* (all clinical rows, by person_id)
             ├─ ehr_service_requests.accession ── pacs_worklist.accession
             └─ ehr_diagnostic_reports.study_uid ── pacs_studies.study_uid
```

- **MPI merges** (ADT^A40 or a reviewed link) call merge hooks registered by the PACS and EHR packages. The hooks repoint every row; encrypted documents are re-encrypted under the surviving id.
- **Across hospitals**, nothing is shared at the database level. Hospitals exchange FHIR R4, DICOM/DICOMweb and HL7 v2, all under signed peer tokens. Because both sides use the same schema and the same identifiers, imports land in identical shapes.
