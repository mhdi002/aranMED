# Security policy

AranMed handles protected health information. Please report vulnerabilities
privately to the repository owner (GitHub: open a *private* security advisory
under **Security → Advisories → Report a vulnerability**). Do not open a
public issue for anything that could expose patient data.

Include what you found, how to reproduce it, and the version (commit) you
tested. We aim to acknowledge reports within five working days.

## Scope

In scope: the backend API (`backend/`), the frontend (`frontend/`), the
DICOM/HL7 listeners, inter-hospital exchange (peer tokens, transfers,
federation), and the deployment files (`Dockerfile`, `docker-compose*.yml`,
`deploy/`).

## Operating securely

The security model — authentication and MFA, roles, peer tokens, consent and
break-the-glass, audit, PHI encryption, network exposure — and the settings
a production deployment must change are described in
[`docs/core/SECURITY.md`](docs/core/SECURITY.md). In short, before exposing
an installation:

- set `ASR_AGENT_SECRET`, `PHI_ENCRYPTION_KEYS` and a unique `FACILITY_OID`;
- keep `ALLOW_ANONYMOUS_AI=false` and `DOCS_ENABLED=false`;
- terminate TLS at the gateway, set `FORWARDED_ALLOW_IPS` to the gateway only;
- restrict the HL7 listener (`HL7_MLLP_ALLOWED_PEERS` or TLS) and DICOM peers
  (`PACS_REQUIRE_KNOWN_PEERS=true`);
- rotate the bootstrap administrator password at first sign-in.

The file `reports/_tok.txt` that used to be tracked held an expired
development token; it has been removed from the tree. If that development
`ASR_AGENT_SECRET` was ever used on a reachable server, rotate it.
