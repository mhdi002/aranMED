# Contributing

## Set-up

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu   # or a CUDA index
pip install -r requirements-dev.txt          # backend + interop + test + lint tools
cd frontend && npm ci && cd ..
pre-commit install                           # ruff, ESLint, whitespace, secrets, large files
```

Node 20 and Python 3.11 are pinned in `.nvmrc` / `.python-version`.

## Before you push

```bash
make lint            # ruff (backend, tests, src) + ESLint with jsx-a11y
make test            # full Python suite on SQLite, with coverage
make test-pg         # backend suite on PostgreSQL   (PG_DSN=postgresql://user@host:port)
make test-mixed      # one hospital on PostgreSQL, the others on SQLite
make e2e             # build the frontend and run Playwright (functional + accessibility)
```

CI (`.github/workflows/ci.yml`) runs the same jobs on every push and pull
request; a change is ready when all of them are green.

## Conventions

- **Tests with the change.** Functional tests start real hospital processes
  (`tests/functional/hospital.py`) and talk real DICOM, HL7 MLLP, FHIR and
  DICOMweb; prefer them for anything that crosses a process or a protocol.
  Security fixes get a regression test in `tests/test_security_hardening.py`.
- **Both languages.** Every user-visible string goes through `t()` with an
  English and a Persian entry (`frontend/lib/i18n*.js`); layout uses logical
  CSS properties so right-to-left works without special cases.
- **Accessibility.** New screens must pass `e2e/a11y.spec.js` (axe-core,
  WCAG 2.1 AA) in both languages.
- **Configuration through the environment.** New settings are documented in
  `.env.example` and `docs/core/CONFIGURATION.md`; no hosts or secrets in code.
- **No PHI in logs, fixtures or commits.** Test data is synthetic.

## Documentation

```bash
make docs            # test reports, EN/FA screenshots, the DOCX guides and the Persian technical document
```

The generators are `scripts/docs/build_docx.js` (user guides) and
`scripts/docs/build_tech_doc_fa.js` (سند فنی); their text lives beside them
in `content.js` and `tech_doc_fa.js`.
