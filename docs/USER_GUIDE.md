# AranMed User Guide

This guide covers every screen in AranMed: the radiology AI workbench, the PACS, the EHR, the links between hospitals, and administration. It also explains how to install, configure, connect and test a hospital.

All screenshots in this guide were taken automatically from a running two-hospital stack (`frontend/e2e/screenshots.spec.js`). See [§9](#9-testing-and-regenerating-the-screenshots) to regenerate them. All patient data in them is synthetic.

**Contents**

1. [Quick start](#1-quick-start)
2. [Signing in, roles and accounts](#2-signing-in-roles-and-accounts)
3. [Reporting workspace](#3-reporting-workspace): dictation, vision chat, reports, templates, EHR, alerts
4. [Imaging (PACS)](#4-imaging-pacs): archive, viewer, worklist, import, DICOM nodes
5. [Clinical (EHR)](#5-clinical-ehr): patients, unified chart, break-the-glass
6. [Between hospitals](#6-between-hospitals): transfers, EMS, interoperability
7. [Education, models and settings](#7-education-models-and-settings)
8. [Language and theme](#8-language-and-theme)
9. [Testing and regenerating the screenshots](#9-testing-and-regenerating-the-screenshots)
10. [Connecting a hospital](#10-connecting-a-hospital)
11. [Troubleshooting](#11-troubleshooting)

Reference documents:

- [`core/PACS_v1.md`](core/PACS_v1.md): PACS services and security
- [`core/EHR_INTEROP_v1.md`](core/EHR_INTEROP_v1.md): EHR, FHIR, HL7, CDA, EMS, federation and transfers
- [`core/CLINICAL_DB_SCHEMA_v1.md`](core/CLINICAL_DB_SCHEMA_v1.md): the shared database
- [`core/CONFIGURATION.md`](core/CONFIGURATION.md): every setting

---

## 1. Quick start

You need:

- Python 3.11 or later
- Node 18 or later
- optionally, Postgres 14 or later; SQLite is the default

```bash
cp .env.example .env                     # every host, port, AE title, secret … lives here
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt      # backend + test dependencies
pip install -e .                         # MedRAG package

# Backend (API, DICOM listener and HL7 listener when enabled in .env)
PYTHONPATH=backend uvicorn app:app --host 0.0.0.0 --port 8010 --app-dir backend

# Frontend
cd frontend && npm install && npm run build
BACKEND_URL=http://127.0.0.1:8010 npm start          # http://127.0.0.1:3000
```

With Docker:

```bash
docker compose up -d --build
```

This starts the gateway, backend, frontend, Postgres, Ollama and `clinical-listeners`. The `clinical-listeners` service is the single DICOM SCP and HL7 MLLP listener, so the API can scale out without fighting over those ports.

**First administrator.** On first start the backend creates the account `ASR_AGENT_ADMIN_USER` (default `admin`):

- If you set `ASR_AGENT_ADMIN_PASSWORD`, that is the password.
- Otherwise a random password is written to `backend/data/initial-admin-password.txt`. The file is deleted at the first successful sign-in.

Sign in as this administrator and create the clinical accounts under **Settings → Users** ([§7](#7-education-models-and-settings)).

**Minimum hospital identity.** Set these in `.env` before connecting a hospital to anyone:

| Setting | Example | Why |
|---|---|---|
| `FACILITY_OID` | `2.25.123…` | Globally unique hospital id. Namespaces MRNs and is checked by peers |
| `FACILITY_NAME` | `Tehran General` | Shown on every record this hospital authors |
| `PUBLIC_BASE_URL` | `https://pacs.hospital.example` | What peers and FHIR `Endpoint`s point to |
| `PHI_ENCRYPTION_KEYS` | (key list) | Encrypts demographics and documents at rest |
| `DATABASE_URL` | `postgresql://…` | Optional. Without it, SQLite is used |

---

## 2. Signing in, roles and accounts

![Role picker](screenshots/01-login-role.png)

Pick the role that matches your work. Each role gets its own workspace and navigation:

| Role | Workspace |
|---|---|
| Radiologist | Dictation, vision chat, PACS (archive, viewer, worklist, import), patients, transfers |
| Doctor | Everything clinical: reporting, EHR and alerts, patients, transfers, EMS, PACS |
| Resident | Clinical workspace with read access to imaging, plus education |
| Student | Education tutor |
| Admin | Everything, plus DICOM nodes, interoperability, model consoles and user management |

The permissions behind each role live in `backend/data/rbac.json`. Edit that file, not Python, to change who can do what.

![Sign-in](screenshots/02-login-credentials.png)

**Who can create an account.** Visitors can register themselves only for the roles listed in `SELF_REGISTER_ROLES`, which defaults to `student`.

For any other role, the sign-in page says that accounts are created by the administrator. The API also refuses these requests with `403`, and every refused attempt is recorded in the audit log.

Doctors, radiologists, residents and admins are created by an administrator in **Settings → Users**. The same thing can be done with `POST /api/auth/register` and an admin token.

Sign-in is protected in three ways:

- repeated failed sign-ins lock the account for a while;
- users can turn on TOTP multi-factor authentication;
- users can list and revoke their own sessions.

---

## 3. Reporting workspace

### Dictation workbench (`/dictate`)

![Dictation](screenshots/10-dictate.png)

1. **Capture.** Record from the microphone or upload an audio file. Persian, English or a mix of both is transcribed by the ASR model.
2. **Transcript & report.** Edit the transcript, then choose **Generate report from transcript**. The core LLM, with MedicalRAG naming rules, drafts a structured report in the institutional template. AranMed picks the template automatically unless you choose one.

   A **template / naming notice** appears when what was dictated doesn't match the selected template. The screenshot shows one: a CT chest dictation was matched to the closest template, *Chest sonography*.
3. **Structured report.** Save it to Reports, copy it, or download it as `.txt`.

### Radiology vision chat (`/radiology`)

![Vision chat](screenshots/11-radiology.png)

Ask knowledge questions, which are answered by MedicalRAG with a radiology focus. You can also attach an image for the local vision model to describe. Quick prompts on the right cover common reads. The model is a drafting assistant, never the only basis for a diagnosis.

### Saved reports (`/reports`) and templates (`/templates`)

![Saved reports](screenshots/12-reports.png)

Saved reports are kept in this browser.

![Templates](screenshots/13-templates.png)

The template browser lists all 56 institutional templates (`backend/data/templates/`). Click a template to preview its normal-report text.

### EHR from free text (`/ehr`) and medication alerts (`/alerts`)

![EHR](screenshots/14-ehr.png)

Paste raw patient information in Persian or English. The core LLM turns it into a structured record: problems, allergies, medications with dosing intervals, and vitals. From the record you can:

- record a dose given (**Record dose**);
- ask questions about the record (MedicalRAG).

Every saved record is also copied into the relational EHR and the Master Patient Index, so it appears in **Patients** too.

![Medication alerts](screenshots/15-alerts.png)

**Check medications** works out which doses are due or were never given. **Send alert** notifies the responsible clinician by email or SMS.

---

## 4. Imaging (PACS)

### Imaging archive (`/pacs`)

![PACS archive](screenshots/20-pacs-browser.png)

- **Top row:** studies, images, storage used, worklist size, and the state of the DICOM listener (AE title and port).
- **Filters:** patient name (wildcards allowed), ID/MRN, accession, modality, date range and status.
- **Where:**
  - *This hospital* searches this PACS only.
  - *Network* searches this PACS, federated DICOM nodes and peer hospitals in parallel. A source that can't be reached is reported but doesn't fail the search.
- **Click a study** to open the side panel: series, priors, linked reports, sign-off buttons, **Send to node** and **Verify integrity** (SHA-256 of every file). Double-click a study, or choose **Open viewer**, to read it.

### Image viewer (`/pacs/viewer`)

![Viewer with a length measurement](screenshots/21-pacs-viewer.png)

The viewer is built on Cornerstone3D.

**Mouse and keyboard**

| Control | Action |
|---|---|
| Left mouse | Active tool |
| Middle mouse | Pan |
| Right mouse | Zoom |
| Wheel or ↑/↓ | Scroll through the stack |
| `i` | Invert |
| `r` | Reset |
| `c` | Cine |

**Toolbar**

- Window/level, with presets: soft tissue, lung, bone, brain, liver, mediastinum
- Pan, zoom, rotate, flip, invert
- Cine, with a frame-rate slider
- Layouts: 1×1, 1×2, 2×2
- **Clear marks**

**Measurements:** Length, Angle, Ellipse and Rectangle ROI with statistics, and Probe in HU. All of them are listed in the **Measurements** tab.

![AI assistant draft](screenshots/22-pacs-viewer-ai.png)

**AI assistant.** **Analyze with AranMed** renders key images, sends them to the vision model, and structures a draft report in the template for that modality and body part. **Ask about this study** answers questions with the study and the patient's earlier studies in context. AI output is always a draft.

![Report sign-off](screenshots/23-pacs-viewer-report.png)

**Report.** The draft opens in the editor. Save it as a draft, mark it *preliminary*, or **Sign final**. A final report is written to the EHR as a DiagnosticReport and marks the study *reported*. Later changes become *amended* versions.

### Modality worklist (`/pacs/worklist`)

![Worklist](screenshots/24-pacs-worklist.png)

Scheduled procedures come from three places:

- HL7 ORM/OMI orders;
- FHIR ServiceRequests;
- the **Order imaging** button in a patient chart, or **Schedule procedure** here.

Modalities pull the list with DICOM MWL C-FIND. **Start** and **Complete** (or the modality's own MPPS messages) move a procedure step through *scheduled → in progress → completed*. Each move is shown under *Performed procedure steps*.

### Import studies (`/pacs/upload`)

![Import](screenshots/25-pacs-upload.png)

Drop DICOM files or ZIP archives, for example from a patient's CD or USB stick. Every image is indexed and matched to the Master Patient Index. The result shows how many images were stored, already present, or rejected, with an **Open viewer** link for each study.

### DICOM nodes & federation (`/pacs/nodes`, admin only)

![DICOM nodes](screenshots/26-pacs-nodes.png)

- **This PACS:** the local AE title, port, listener state and allow-list mode.
- **DICOM nodes:** modalities, other PACS (DIMSE), DICOMweb archives and Orthanc servers. Each node has these settings:
  - store, query and retrieve permissions;
  - whether it is a *move destination*;
  - whether it takes part in network searches (*federate*);
  - optionally, the name of an env var that holds its credentials.

  **Echo** runs a live C-ECHO (DIMSE) or a reachability check (web).
- **Query — remote:** C-FIND or QIDO on a single node, with **Retrieve** for each result.
- **Transfer jobs:** retrieve and send jobs, with status, results and retry errors.

---

## 5. Clinical (EHR)

### Patients (`/clinical`)

![Patient search](screenshots/30-clinical-search.png)

There is one record per person across this hospital and the network. Search by name, MRN, national ID or birth date. Tick **Include other hospitals** to also run IHE PDQm matching against peer hospitals.

![Register patient](screenshots/31-clinical-register.png)

**Register patient** creates the identity in the Master Patient Index. A confident match to an existing person links to that person rather than creating a new one. A weaker match creates the new person and puts the pair in the identity review queue ([§6](#interoperability-interop-admin-only)).

### Unified patient chart (`/clinical/chart`)

![Chart summary](screenshots/32-chart-summary.png)

- **Header:** identifiers (national ID and every MRN), **Records from other hospitals**, **Export CCD** (C-CDA R2.1) and **Transfer patient**.
- **Summary:** allergies, active problems, active medications, latest vitals and abnormal results, the last encounter, the number of imaging studies, and **Ask about this patient**. The question is answered by the agent with the whole chart in context.

![Chart including a peer hospital's records](screenshots/33-chart-with-peer-records.png)

Tick **Records from other hospitals** to fetch the patient's records live from peer hospitals. AranMed matches the patient by national ID or MRN, falling back to a PDQm match.

Every item carries a **source badge**:

- grey `local` for this hospital;
- orange, with the hospital's name, for a peer.

In the screenshot, the contrast allergy and the clopidogrel prescription are held only at *E2E Peer Hospital*, yet both appear in this hospital's chart. The access is audited at both hospitals.

**Tabs**

| Tab | Shows |
|---|---|
| Encounters | Visits: ambulatory, emergency (including EMS pre-arrival) and inpatient |
| Results | Labs and vitals with reference ranges, abnormal flags and a trend line |
| Medications | Statements and prescriptions, with dose, route, frequency and history |
| Documents | Notes, discharge summaries and ePCRs, encrypted at rest |
| Imaging | Local and peer studies. **Retrieve** fetches a peer study into this PACS, then **Open viewer** |
| Orders | Service requests. **Order imaging** creates the worklist entry with an accession number |
| Timeline | Everything in date order, with its source |
| Consent & access | Consents (restricted, deny-sharing, permit-sharing), how access was granted, and the patient's transfers |

![Results](screenshots/34-chart-results.png)
![Imaging](screenshots/35-chart-imaging.png)
![Documents](screenshots/36-chart-documents.png)
![Orders](screenshots/37-chart-orders.png)
![Encounters](screenshots/38-chart-encounters.png)
![Medications](screenshots/38-chart-medications.png)
![Timeline](screenshots/38-chart-timeline.png)
![Consent & access](screenshots/38-chart-access.png)

### Restricted records (break-the-glass)

![Restricted record](screenshots/39-chart-restricted.png)

A record with a *restricted* consent stays hidden until a clinician with `clinical.breakglass` does three things:

1. enters a reason;
2. chooses **Break the glass**;
3. accepts access for a limited time (`BREAKGLASS_MINUTES`).

The grant and the reason are audited and reviewed. The AI agent can report that break-the-glass is needed, but it can never break the glass itself.

---

## 6. Between hospitals

### Inter-hospital transfers (`/transfers`)

![Transfers](screenshots/40-transfers.png)

Each transfer appears as a card in **Incoming** or **Outgoing**. A card shows the patient, the two hospitals, the urgency, the reason, a clinical summary, the transport and a status bar. Each hospital keeps its own copy of the transfer and pushes every change to the other:

1. **Request.** The sending hospital uses **Transfer patient** in the chart. The patient is pre-registered at the receiving hospital.
2. **Accept or reject.** On accept, the sending hospital automatically pushes the full record (a FHIR transaction) and the images (STOW-RS). *Record package* shows what arrived, by resource type.
3. **In transit → arrived → completed.**

Sending the package again never creates duplicates.

### EMS inbound (`/ems`)

![EMS board](screenshots/41-ems.png)

Ambulances post pre-arrival reports as NEMSIS v3.5 (XML or JSON) or as FHIR bundles to `POST /api/ems/notify`. Each report creates:

- the patient identity;
- an emergency encounter;
- vitals and treatments;
- the ePCR document;
- this board card, with abnormal vitals flagged and a MIST handover;
- alerts to the ED (`EMS_ALERT_EMAIL`, `EMS_ALERT_SMS`).

The ED moves each card through **Acknowledge → Arrived → Handed over**.

### Interoperability (`/interop`, admin only)

![Interoperability](screenshots/42-interop.png)

- **Status:** this facility, the FHIR base, the DICOM listener, the HL7 MLLP listener and the schema versions. Peers compare schema versions before exchanging data.
- **Peer facilities:** add or update peers, each with an OID, endpoints, trust level and the name of the env var holding the shared secret.

  **Test** checks three things: *reachable*, *schema* compatible, and *token* accepted.
- **Identity review queue:** possible duplicate patients with their match score. **Same person** merges them, and all PACS and EHR rows follow the merge. **Different people** keeps them apart.
- **Send HL7 v2 message:** paste an ADT, ORM, ORU, MDM, VXU or QBP message and see the ACK.
- **Import CCD:** load a C-CDA document into the matching patient's chart.
- **Message log:** every HL7, FHIR, DICOM, transfer and EMS exchange, in and out. Filter by protocol and open the raw message.

---

## 7. Education, models and settings

![Education tutor](screenshots/50-education.png)

The **education tutor** generates multiple-choice questions, case studies, mock exams and explanations of concepts by topic and difficulty. Generated sets can be saved.

![Speech model](screenshots/51-asr.png)
![Language model](screenshots/52-llm.png)
![Vision model](screenshots/53-vision.png)

The **model consoles** (admin only) show which model serves each role (speech, core LLM, vision), with its provider, status and warm-up state. These all come from the model registry (`backend/models.yaml`), not from code.

![Settings and user management](screenshots/54-settings-users.png)

**Settings:**

- theme;
- default ASR language hint;
- **Reset workspace**, which clears data saved in this browser;
- the backend endpoints in use.

Admins also see **Users**: every account with its role and MFA state, and a form to create new accounts with any role.

---

## 8. Language and theme

Use **EN / FA** in the top bar to switch between English and Persian. In Persian the whole interface switches to right-to-left, including navigation, tables, the chart and the EMS board. **Dark / Light** switches the theme. Both choices are remembered in the browser.

![Persian — PACS](screenshots/60-fa-pacs.png)
![Persian — chart](screenshots/61-fa-chart.png)
![Persian — EMS](screenshots/62-fa-ems.png)
![Dark — viewer](screenshots/70-dark-viewer.png)
![Dark — chart](screenshots/71-dark-chart.png)

---

## 9. Testing and regenerating the screenshots

```bash
pytest                                   # whole Python suite: unit + functional + MedRAG
DATABASE_URL=postgresql://user@host:5432/db pytest --ignore=tests/medrag   # backend on Postgres
cd frontend && npm run build
npm run test:e2e                         # browser journeys against a seeded two-hospital stack
E2E_SCREENSHOTS=1 npx playwright test screenshots   # regenerate docs/screenshots/*.png
```

- **Functional tests** (`tests/functional/`) start real hospital processes and talk to them over real DICOM (DIMSE), DICOMweb, FHIR, HL7 MLLP and peer-token federation.
- **The E2E stack** (`tests/e2e/stack.py`) runs three things:
  - a deterministic fake LLM, so AI features work without a GPU;
  - two complete hospitals, each with its own database and PACS;
  - seed data for the scenarios above.
- Set `E2E_CHROMIUM` to use a Chromium you already have.
- Run the screenshot spec on its own, so the seed is fresh. For example, the transfer should still be pending and the ambulance still inbound.

---

## 10. Connecting a hospital

1. **Identity.** Set `FACILITY_OID`, `FACILITY_NAME`, `PUBLIC_BASE_URL`, `PACS_AE_TITLE` and `PHI_ENCRYPTION_KEYS`.
2. **DICOM.** Set `PACS_DIMSE_ENABLED=true` on exactly one process; in Docker that is the `clinical-listeners` service. Also set `PACS_DIMSE_PORT`. Then register every modality and remote PACS in **DICOM Nodes**. With `PACS_REQUIRE_KNOWN_PEERS=true` (the default), unknown AE titles are refused. Use `PACS_DIMSE_TLS_*` for TLS or mutual TLS.
3. **HL7.** Set `HL7_MLLP_ENABLED=true`, `HL7_MLLP_PORT` and `HL7_MLLP_ALLOWED_PEERS`. Point the HIS/RIS at the listener.
4. **Peer hospitals.** Agree a shared secret and store it in an env var, for example `PEER_TEHRAN_SECRET`. Add the peer in **Interoperability**, using `secret_env=PEER_TEHRAN_SECRET`, or list it in `FACILITY_PEERS_FILE`. Press **Test** and wait for three green checks. The other hospital does the same with your details.
5. **Sharing policy.** `CONSENT_SHARING_DEFAULT` is `opt-out` (the default) or `opt-in`. Per-patient consents override it. Emergency-treatment purpose (`ETREAT`) is always audited at both hospitals.
6. **EMS agencies** post to `/api/ems/notify` with a user or peer token.

All other settings are in `.env.example` and [`core/CONFIGURATION.md`](core/CONFIGURATION.md).

---

## 11. Troubleshooting

| Symptom | Check |
|---|---|
| A modality's C-STORE is rejected | Its AE title isn't an active node with *store* allowed, or `PACS_REQUIRE_KNOWN_PEERS` is on (see **DICOM Nodes**) |
| C-MOVE goes nowhere | The destination must be a registered node marked *move destination* |
| A peer's **Test** shows *token* red | The two hospitals hold different shared secrets, or a peer is registered under the wrong OID |
| A peer's **Test** shows *schema* red | The two hospitals run different AranMed versions. Upgrade the older one |
| Peer records don't appear in the chart | **Records from other hospitals** is off, the patient has no national ID or MRN in common with the peer, or the peer's consent policy withholds them. A notice says so; results are never silently empty |
| "Accounts with role X are created by your administrator" | That role isn't in `SELF_REGISTER_ROLES`. An admin creates the account in **Settings → Users** |
| `pytest` stops on duplicate test module names | Use the repo's `pytest.ini`, which sets `--import-mode=importlib` |
| Viewer shows a blank image | The study uses a compressed transfer syntax. The viewer falls back to server-rendered frames; see the backend log for decode errors |
