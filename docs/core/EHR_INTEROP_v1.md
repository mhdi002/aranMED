# AranMed EHR & Interoperability v1

Code: `backend/ehr/`, `backend/interop/`, `backend/clinicaldb/` · UI: `frontend/pages/{clinical,transfers,ems,interop}` · Tests: `tests/functional/test_{ehr_clinical,fhir_server,hl7_mllp,cda_ems,interhospital,clinical_agent}.py`, `frontend/e2e/clinical.spec.js`

## 1. The record

The EHR is relational and FHIR-aligned; see [`CLINICAL_DB_SCHEMA_v1.md`](CLINICAL_DB_SCHEMA_v1.md). It is keyed by the MPI person, and every row remembers which facility authored it.

**The unified chart** (`ehr/chart.py`, `GET /api/clinical/patients/{id}/chart`) combines:

- the local record;
- the PACS imaging;
- optionally, **live records from peer hospitals** (`include_remote=true`).

Each item carries a `source` field: the facility, plus whether the record is held locally or fetched from a peer.

**The legacy JSON EHR still works.** The dictation and agent pipeline keeps writing it exactly as before. `ehr/bridge.py`, an after-save hook on `store.upsert_patient`, projects each saved record into the MPI and the relational tables.

**Access control:**

- **Local users:** RBAC applies (`clinical.read` and `clinical.write`). A *restricted* record (Consent category `restricted`) needs break-the-glass: `clinical.breakglass`, a stated reason, a time limit (`BREAKGLASS_MINUTES`) and an audit entry.
- **Peers:** sharing follows `CONSENT_SHARING_DEFAULT`, either `opt-out` (the default) or `opt-in`, together with `deny-sharing` and `permit-sharing` consents. A peer declaring purpose `ETREAT` (emergency treatment) overrides a refusal, and that override is audited at both hospitals. When consent withholds a patient from a search, the response includes an OperationOutcome saying a match was withheld, so the result is never silently empty.

## 2. Protocols

| Protocol | Where | Scope |
|---|---|---|
| **FHIR R4** | `/api/fhir/r4` (`fhir_server.py`, `fhir_map.py`) | metadata, read, vread, history, search, create, update (If-Match), delete; transaction and batch with `urn:uuid` resolution and rollback; `Patient/$everything`, `$summary` (IPS-style document), `$match` (IHE PDQm), `$ihe-pix` (IHE PIXm); IHE MHD via DocumentReference. Resources: Patient, Encounter, Condition, AllergyIntolerance, MedicationStatement, MedicationRequest, Observation, Procedure, Immunization, DocumentReference, ServiceRequest, DiagnosticReport, Consent, ImagingStudy, Organization, Endpoint. Every emitted resource is validated against the official R4B models in the test suite |
| **HL7 v2** | MLLP (`mllp.py`) and `POST /api/interop/hl7` | ADT A01, A02, A03, A04, A05, A08, A11, A13, A28, A31 and A40 (merge); ORM, OMI and OMG orders (imaging orders go to the worklist, ORC CA cancels); ORU results (lab and radiology narrative); MDM documents; VXU immunizations; QBP Q22 and Q23 (IHE PDQ and PIX) → RSP. Each message is answered AA, AE or AR and logged |
| **C-CDA R2.1** | `GET /api/interop/cda/{id}`, `POST /api/interop/cda` | CCD export (narrative plus coded entries for problems, allergies, medications, results, vitals, procedures, immunizations and encounters) and import, with source attribution and de-duplication |
| **DICOM / DICOMweb** | see [`PACS_v1.md`](PACS_v1.md) | images within and between hospitals |
| **EMS** | `POST /api/ems/notify`, `/api/ems/board` | NEMSIS v3.5 XML or JSON, and FHIR EMS bundles. A report creates the MPI identity, a pre-arrival EMER encounter, vitals, treatments, the ePCR document, an ED board card with a MIST handoff, and alerts (`EMS_ALERT_EMAIL`, `EMS_ALERT_SMS`) |

## 3. Between hospitals

**Trust.** Each peer is registered in `facilities` with its endpoints, a `trust_level` and a `secret_env`. Requests carry short-lived HS256 **peer tokens** (`clinicaldb/principal.py`) with these claims:

- `iss` and `aud`: facility OIDs;
- `sub`: the practitioner;
- `purpose`: HL7 PurposeOfUse (TREAT, ETREAT, TRANSFER, …).

The receiver verifies the token, applies `peer_policy.json` and consent, and audits the request.

**Discovery and remote chart** (`interop/federation.py`). The patient is located at each peer by shared identifiers: the national id first, then any MRN that peer issued. If none match, a PDQm `$match` is tried, and only certain or probable results are accepted. A deterministic match cross-references the peer's identifiers into the local MPI, so imaging fetched later lands on the same person. The peer's records then arrive through `Patient/$everything`.

**Lossless exchange.** Every EHR field travels, so the receiving hospital gets the same record. The FHIR mapping (`fhir_map.py`) carries:
- medication dose, route, timing, interval and prescriber as structured `Dosage`;
- encounter department and attending;
- performers, report result links, notes and the row `data` column, in AranMed extensions where FHIR has no slot.

`tests/test_fhir_map_roundtrip.py` checks every type round-trips field-for-field and validates as FHIR R4B.

**Transfers** (`interop/transfers.py`). Each hospital keeps its own transfer row, and every state change is pushed to the other side:

1. The sending hospital requests the transfer, and the patient is pre-registered at the receiving hospital.
2. The receiving hospital accepts. The package goes out automatically: a FHIR transaction with the full record and a transfer summary, plus imaging over STOW-RS.
3. The sending hospital marks the patient as departed.
4. The receiving hospital records arrival, then completion.

Re-sending a package is a no-op, because records de-duplicate on `(source_facility, source_id)`. A failed transaction never rolls back rows that already existed.

The package also keeps several things intact:
- References inside the package (encounters, report results, document context) are `urn:uuid` links, so they resolve to the receiver's own rows.
- Medications bring their **dose-change history**, which is replayed into the receiver's history.
- The package always lands on the transfer's patient.
- Another hospital's imaging orders stay as history; they are not placed on the receiver's worklist.

Remote documents are read on demand (`/api/clinical/patients/{id}/remote-documents/{facility}/{doc}`). The holding hospital applies its consent rules and audits the read.

`tests/functional/test_interhospital_records.py` runs four real hospitals and covers:
- a full-record transfer to a new hospital, and to one where the patient already exists;
- idempotent re-sends and failure paths;
- a chain A→B→C with authorship kept;
- a visiting patient seen live;
- consent on records and images (wildcard, opt-in, ETREAT audited at both ends);
- an MPI merge after a transfer;
- the agent's tools across hospitals.

**Operations.** `POST /api/interop/peers/test` checks that a peer is reachable, that its schema versions are compatible, that its OID matches, and that it accepts our token. The interop message log keeps every exchange.

## 4. Agent

`tools/clinical.py` provides: `clinical_patient_search` (local or network), `clinical_patient_summary` (the unified chart, including peer hospitals, with the source of each item), `clinical_timeline`, `request_patient_transfer`, `transfer_status` and `ems_inbound`.

`/api/clinical/patients/{id}/ask` runs the agent with the chart in its context.

The tools enforce the same RBAC, access decisions and audit as the UI. For a restricted record, the agent reports that break-the-glass is needed; it cannot break the glass on the user's behalf.
