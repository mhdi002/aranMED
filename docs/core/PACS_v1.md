# AranMed PACS v1

Code: `backend/pacs/` · UI: `frontend/pages/pacs/*` · Tests: `tests/functional/test_pacs_*.py`, `frontend/e2e/pacs.spec.js`

## 1. What it is

A complete picture archiving and communication system inside AranMed. It receives images from modalities and other PACS, indexes them against the Master Patient Index, serves them to the in-app viewer and to other hospitals, and connects them to the EHR and the AI agent.

```
modality ──C-STORE/MWL/MPPS──▶ DIMSE SCP ─┐
other PACS ─C-MOVE/C-FIND/C-GET─▶          ├─▶ ingest ─▶ storage (Part 10 files, SHA-256)
browser/CD ──upload/STOW-RS──▶ DICOMweb ──┘            └▶ index (studies/series/instances ⟷ MPI)
viewer ◀── QIDO / WADO-RS (metadata, frames, rendered) ──┘
peers  ◀──▶ adapters (DICOMweb · Orthanc REST · DIMSE) · federation · retrieve/send jobs
agent  ◀── tools/pacs.py (search, analyze, priors, reports, worklist)
```

## 2. Services

| Area | Implemented |
|---|---|
| DIMSE (`dimse.py`) | C-ECHO; C-STORE for every storage SOP class and transfer syntax, plus configured vendor-private classes; C-FIND, C-MOVE and C-GET (Patient Root and Study Root); Modality Worklist C-FIND; MPPS N-CREATE and N-SET (duplicate → 0x0111); Storage Commitment N-ACTION, answered by an N-EVENT-REPORT on a new association. C-MOVE and C-GET **transcode** to Explicit VR LE when the receiver does not accept the stored compressed syntax (same SOP Instance UID). TLS / mTLS on the SCP; called AE title and association limit enforced |
| DIMSE SCU (`scu.py`) | echo, find, MWL find, move, get, store to any node. C-GET offers exactly the SOP classes present in the study with all transfer syntaxes. C-STORE splits large sets across associations and transcodes when needed. Queries carry `SpecificCharacterSet` for Persian names. TLS / mTLS per node |
| DICOMweb (`dicomweb.py`, at `/api/dicom-web`) | QIDO-RS (studies, series, instances; wildcards, ranges, UID lists, `includefield`, paging); WADO-RS (study, series and instance multipart; `/metadata` with BulkDataURI; `/frames` as stored, **transcoded** to Explicit VR LE on request, or normalised for the viewer with `?normalize=1`; `/rendered` and `/thumbnail` with `window=`; `/bulk`); `/sr` (Structured Report as text), `/document` (Encapsulated PDF/CDA/STL/OBJ), `/video` (MPEG-2/H.264/HEVC stream); STOW-RS with a standard store response; WADO-URI. Undecodable data → 406 with the reason, never 500 |
| Management (`/api/pacs`) | browse with friendly filters; upload of files and zips; study detail with series, priors, order and reports; status; delete (admin); integrity verify; nodes CRUD and echo; remote query; retrieve and send jobs; worklist; MPPS; stats; AI analyze and ask |
| Adapters | `DicomwebAdapter` (dcm4chee-arc, Orthanc DICOMweb plugin, cloud APIs, peer AranMed with peer tokens); `OrthancAdapter` (Orthanc REST); `DimseAdapter` (C-FIND, then C-MOVE to our AE or C-GET) |
| Federation | parallel search across the local index, `federate` nodes and peer facilities; results merged by Study UID with every location listed; an unreachable source degrades the result instead of failing it |
| Workflow | order (HL7 ORM, FHIR ServiceRequest or UI) → worklist entry with a pre-assigned Study UID → MWL → MPPS in progress → images → MPPS completed → report (draft → preliminary → final → amended) → EHR DiagnosticReport |

## 3. Devices and transfer syntaxes

Decoding uses pydicom's pixel handlers with Pillow, pylibjpeg (libjpeg, openjpeg), pyjpegls (CharLS) and GDCM (`backend/requirements-interop.txt`).

| Transfer syntax | Store | Index | View |
|---|---|---|---|
| Implicit / Explicit VR Little Endian, Explicit VR Big Endian, Deflate | ✓ | ✓ | ✓ native values |
| RLE Lossless (8/16/32-bit) | ✓ | ✓ | ✓ |
| JPEG Baseline, Extended (12-bit), Lossless (Process 14, SV1) | ✓ | ✓ | ✓ |
| JPEG-LS lossless / near-lossless | ✓ | ✓ | ✓ |
| JPEG 2000 (lossless / lossy), HTJ2K | ✓ | ✓ | ✓ |
| MPEG-2, H.264, HEVC video | ✓ | ✓ | stream via `/video` |

| Object | What the viewer shows |
|---|---|
| CT / MR / PT / NM / DX / MG / CR / XA / RF | stored values with rescale (HU for CT), per-frame for Enhanced multi-frame; MONOCHROME1 inverted |
| US / endoscopy / SC (RGB, YBR, planar configuration 1), PALETTE COLOR | true colour (palette expanded; YBR converted once) |
| SEG (1-bit), RT Dose (32-bit, grid scaling) | unpacked / scaled values |
| SR, Encapsulated PDF/CDA/STL/OBJ, video, ECG waveform, RT Plan/Struct | listed under the study as objects: SR as text, PDF/video in a viewer, others downloadable |

Text without a declared character set (older local devices) is indexed as UTF-8 or `PACS_FALLBACK_CHARSET` (default cp1256). Name matching folds Persian/Arabic letter variants, so `علی*` finds `علي`. Worklist times are stored as DICOM date-times whatever the source (HL7, FHIR, UI), so MWL date queries match orders.

`tests/functional/test_pacs_devices.py` proves each row:
- real-world files from pydicom's corpus;
- synthetic devices in every syntax they use, compared pixel-for-pixel;
- a pure-red patch checks colour;
- transcoding, TLS, worklist dates, character sets, limits, and media import.

## 4. Security

- **Patient consent applies to images.** Peer hospitals using DICOMweb, and DICOM nodes that carry the `facility_oid` of another hospital, only see studies whose patient's sharing consent allows it. ETREAT overrides are audited, and so are denials.
- **AE allow-list.** With `PACS_REQUIRE_KNOWN_PEERS=true` (the default), only active `pacs_nodes` AE titles can associate. Each node then carries `allow_store`, `allow_query` and `allow_retrieve`.
- **C-MOVE destinations** must be registered nodes flagged `is_move_destination`. The PACS will not send studies to an arbitrary AE title.
- **DICOMweb and `/api/pacs`** accept a user session (`pacs.read`, `pacs.write`, `pacs.admin` in `rbac.json`) or a signed peer token (`peer_policy.json`). Reads, searches, retrievals and stores are all audited, and peer requests record the practitioner and purpose of use.
- **Storage** keeps Part 10 files as received; integrity is checked by SHA-256 (`/verify`). Protect the volume with disk or volume encryption, because DICOM headers carry PHI.

## 5. Viewer (`frontend/components/pacs/`)

The viewer uses Cornerstone3D stack viewports with a custom image loader, `aranmed:`. It reads WADO-RS metadata and `/frames/N?normalize=1`, which the server delivers decoded from any transfer syntax, with each frame's rescale and window in response headers. This avoids WASM codecs and web workers in the Next.js build. If a frame cannot be decoded, the server's `/rendered` PNG is shown instead.

- **Image tools:** W/L with presets (soft tissue, lung, bone, brain, liver, mediastinum), pan, zoom, scroll, cine, invert, rotate, flip, reset.
- **Measurements:** Length, Angle, Ellipse and Rectangle ROI with statistics, and Probe in HU, because the modality LUT is pre-applied.
- **Layout and panels:** 1×1, 1×2 and 2×2 layouts with a series picker. A side panel shows study info, priors, report sign-off, the AI analyze and ask functions, and measurements.

## 6. Deployment

In Docker, the DICOM SCP and the HL7 MLLP listener run in the singleton `clinical-listeners` service, because a port can only be bound once while the API is scaled. Bare-metal deployments set `PACS_DIMSE_ENABLED=true` on one backend.

All settings (`PACS_*`, `DICOMWEB_PREFIX`, `DICOM_UID_ROOT`, …) are listed in `.env.example`.
