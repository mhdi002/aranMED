"""Remote DICOM nodes: modalities, other PACS, DICOMweb/Orthanc peers.

``kind`` decides how we talk to a node:

* ``dimse``    — classic DICOM networking (AE title / host / port). Modalities
  and most hospital PACS.
* ``dicomweb`` — QIDO/WADO/STOW over HTTP (dcm4chee-arc, Orthanc's DICOMweb
  plugin, Google/Azure health APIs, another AranMed).
* ``orthanc``  — Orthanc's native REST API.

For inbound DIMSE, the node row is also the allow-list: with
``PACS_REQUIRE_KNOWN_PEERS`` on (the default) an association from an AE
title that is not registered and active is rejected, and ``allow_store`` /
``allow_query`` / ``allow_retrieve`` gate what it may do. Credentials for
HTTP nodes are referenced by environment-variable name (``auth_env``), the
value being ``Bearer <token>`` or ``Basic <b64>`` or ``user:password``.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import db
from clinicaldb.util import new_id, row
from pacs import schema  # noqa: F401

FIELDS = ("name", "kind", "ae_title", "host", "port", "base_url", "auth_env", "facility_oid",
          "issuer", "allow_store", "allow_query", "allow_retrieve", "is_move_destination",
          "active", "federate", "tls", "tls_ca_env", "tls_cert_env", "tls_key_env", "prefer_cget",
          "charset")
_BOOL = ("allow_store", "allow_query", "allow_retrieve", "is_move_destination", "active",
         "federate", "tls", "prefer_cget")


def _out(r) -> Optional[dict]:
    d = row(r)
    if d is None:
        return None
    for k in _BOOL:
        d[k] = bool(d.get(k))
    d["has_credentials"] = bool(d.get("auth_env") and os.environ.get(d["auth_env"]))
    return d


def list_nodes(*, kind: Optional[str] = None, active_only: bool = False) -> list[dict]:
    sql, params = "SELECT * FROM pacs_nodes", []
    clauses = []
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    if active_only:
        clauses.append("active=1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    with db.connect() as c:
        return [_out(r) for r in c.execute(sql + " ORDER BY name", params).fetchall()]


def get(node_id: str) -> Optional[dict]:
    with db.connect() as c:
        return _out(c.execute("SELECT * FROM pacs_nodes WHERE id=? OR name=?",
                              (node_id, node_id)).fetchone())


def by_ae(ae_title: str) -> Optional[dict]:
    ae = (ae_title or "").strip()
    with db.connect() as c:
        return _out(c.execute("SELECT * FROM pacs_nodes WHERE ae_title=? AND active=1 "
                              "ORDER BY kind='dimse' DESC LIMIT 1", (ae,)).fetchone())


def save(data: dict[str, Any], node_id: Optional[str] = None) -> dict:
    kind = data.get("kind") or "dimse"
    if kind not in ("dimse", "dicomweb", "orthanc"):
        raise ValueError("kind must be dimse, dicomweb or orthanc")
    if not data.get("name"):
        raise ValueError("name is required")
    if kind == "dimse" and not (data.get("ae_title") and data.get("host") and data.get("port")):
        raise ValueError("a DIMSE node needs ae_title, host and port")
    if kind in ("dicomweb", "orthanc") and not data.get("base_url"):
        raise ValueError(f"a {kind} node needs base_url")
    values = {k: data[k] for k in FIELDS if k in data}
    for k in _BOOL:
        if k in values:
            values[k] = 1 if values[k] in (True, 1, "1", "true") else 0
    now = db.now()
    with db.connect() as c:
        if node_id and c.execute("SELECT 1 FROM pacs_nodes WHERE id=?", (node_id,)).fetchone():
            sets = ", ".join(f"{k}=?" for k in values)
            c.execute(f"UPDATE pacs_nodes SET {sets}, updated_at=? WHERE id=?",
                      (*values.values(), now, node_id))
        else:
            node_id = node_id or new_id()
            cols = list(values) + ["id", "created_at", "updated_at"]
            c.execute(f"INSERT INTO pacs_nodes({', '.join(cols)}) VALUES "
                      f"({', '.join('?' for _ in cols)})", (*values.values(), node_id, now, now))
    return get(node_id)  # type: ignore[return-value]


def delete(node_id: str) -> bool:
    with db.connect() as c:
        return c.execute("DELETE FROM pacs_nodes WHERE id=?", (node_id,)).rowcount > 0


def record_echo(node_id: str, ok: bool) -> None:
    with db.connect() as c:
        c.execute("UPDATE pacs_nodes SET last_echo_at=?, last_echo_ok=? WHERE id=?",
                  (db.now(), 1 if ok else 0, node_id))


def tls_context(node: dict, *, server_side: bool = False):
    """ssl.SSLContext for a node with ``tls`` on. Certificate and key paths
    are named by env vars on the node (never stored in the database):
    ``tls_ca_env`` (trust anchor), ``tls_cert_env`` + ``tls_key_env`` (our
    client certificate, for mutual TLS)."""
    if not node.get("tls"):
        return None
    import ssl
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ca = os.environ.get(node.get("tls_ca_env") or "", "")
    if ca:
        ctx.load_verify_locations(ca)
    cert = os.environ.get(node.get("tls_cert_env") or "", "")
    key = os.environ.get(node.get("tls_key_env") or "", "")
    if cert and key:
        ctx.load_cert_chain(cert, key)
    # DICOM peers are addressed by IP and AE title far more often than by a
    # DNS name in their certificate; identity is the CA-signed certificate.
    ctx.check_hostname = False
    return ctx


def auth_header(node: dict) -> dict[str, str]:
    raw = os.environ.get(node.get("auth_env") or "", "").strip()
    if not raw:
        return {}
    if raw.lower().startswith(("bearer ", "basic ")):
        return {"Authorization": raw}
    if ":" in raw:
        import base64
        return {"Authorization": "Basic " + base64.b64encode(raw.encode()).decode()}
    return {"Authorization": f"Bearer {raw}"}
