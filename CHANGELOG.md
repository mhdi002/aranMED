# Changelog

## Round 4 — hardening and polish

**Security**
- AI endpoints require a signed-in user; anonymous agent turns never reach patient records.
- Template ids confined to the template directory (path traversal fixed).
- MFA re-enrolment needs proof; TOTP and peer-token replay guards shared across replicas.
- Conversations bound to their owner.
- Transfer packages limited to creating the transferred patient's records; FHIR transactions roll back updates and deletes.
- HL7 listener safe by default (loopback, allowlist/TLS required, idle timeout).
- PACS management API for users only; restricted records gate imaging.
- Real client addresses behind proxies; per-address login budget; throttled sign-up.
- Password change, account disable/role change/reset; tokens end with either.
- Recovery codes salted per code; TOTP seeds and conversations encrypted with the PHI key.
- Dependency upgrades (FastAPI/Starlette, python-multipart, requests); non-root containers; loopback publishing for internal services; CSP and security headers on the frontend.

**Reliability**
- Blocking work off the event loop; bounded uploads; PostgreSQL connection pool.
- Readiness probe, request ids in logs and responses, optional JSON logs and Prometheus metrics.

**Frontend**
- One API client with session-expiry handling.
- Accessible components (labels, dialogs, tabs, drawer, skip link), WCAG AA colours, full Persian translation.
- Confirmations for irreversible actions; toasts.

**Developer experience**
- ruff and ESLint (jsx-a11y) clean.
- GitHub Actions CI (lint, SQLite, PostgreSQL, mixed network, E2E with accessibility audit, image build).
- pre-commit, Dependabot, Makefile targets.

**Documentation**
- Persian technical document (`docs/سند فنی.docx`), SECURITY, CONTRIBUTING, refreshed guides.

## Round 3 — EHR hub, lossless exchange, every device

- One EHR section with tabs (patients, transfers, EMS, medication alerts, text intake).
- Records survive moving between hospitals intact:
  - lossless FHIR mapping, including dose and dose history;
  - transfer relinking, idempotency and failure paths;
  - tested across four hospital processes and mixed databases.
- PACS decodes every common transfer syntax and colour model, transcodes for receivers, shows SR, PDF and video, and supports TLS, Persian names and legacy character sets.
- Bilingual DOCX guides with screenshots and test results.

## Round 2 — finalisation

- Privileged roles no longer self-register; administrator user management.
- Full verification on SQLite and PostgreSQL; screenshots of every page and a user guide.

## Round 1 — clinical platform

- Shared clinical database, facility registry and Master Patient Index.
- PACS: DIMSE, DICOMweb, worklist/MPPS, Cornerstone3D viewer, AI reads.
- Relational EHR and interoperability:
  - FHIR R4, HL7 v2 over MLLP, C-CDA, NEMSIS EMS;
  - federation, transfers, consent and break-the-glass.
- UI and E2E tests.
