"""Facility registry: this hospital plus the peers it exchanges data with.

The local row is (re)seeded from environment variables at start-up
(``FACILITY_*``, ``PACS_AE_TITLE`` …) so a deployment's identity lives in its
config, not in code. Peers are registered through ``/api/facilities`` (admin)
or seeded from a JSON file named by ``FACILITY_PEERS_FILE``.

A peer's shared secret is never stored: ``secret_env`` names the environment
variable that holds it, so rotating a secret is a config change and a DB
dump leaks nothing usable.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import db
from clinicaldb import schema  # noqa: F401  (registers tables)
from clinicaldb import settings
from clinicaldb.util import jdump, jload, new_id, row

log = logging.getLogger("clinicaldb.facilities")

FIELDS = ("oid", "name", "kind", "ae_title", "dicom_host", "dicom_port",
          "base_url", "fhir_base", "dicomweb_base", "mllp_host", "mllp_port",
          "auth_mode", "secret_env", "trust_level", "active")


def _out(r) -> Optional[dict]:
    d = row(r)
    if d is None:
        return None
    d["meta"] = jload(d.get("meta"), {})
    d["is_local"] = bool(d.get("is_local"))
    d["active"] = bool(d.get("active"))
    d["has_secret"] = bool(d.get("secret_env") and os.environ.get(d["secret_env"]))
    return d


def get_by_oid(oid: str) -> Optional[dict]:
    with db.connect() as c:
        return _out(c.execute("SELECT * FROM facilities WHERE oid=?", (oid,)).fetchone())


def get(facility_id: str) -> Optional[dict]:
    with db.connect() as c:
        return _out(c.execute("SELECT * FROM facilities WHERE id=? OR oid=?",
                              (facility_id, facility_id)).fetchone())


def list_all(*, include_inactive: bool = True) -> list[dict]:
    sql = "SELECT * FROM facilities"
    if not include_inactive:
        sql += " WHERE active=1"
    sql += " ORDER BY is_local DESC, name"
    with db.connect() as c:
        return [_out(r) for r in c.execute(sql).fetchall()]


def peers(*, kind: Optional[str] = None) -> list[dict]:
    out = [f for f in list_all(include_inactive=False) if not f["is_local"]]
    if kind:
        out = [f for f in out if f["kind"] == kind]
    return out


def upsert(data: dict[str, Any], *, is_local: bool = False) -> dict:
    oid = (data.get("oid") or "").strip()
    name = (data.get("name") or "").strip()
    if not oid or not name:
        raise ValueError("facility needs 'oid' and 'name'")
    now = db.now()
    values = {k: data.get(k) for k in FIELDS if k in data}
    values["oid"], values["name"] = oid, name
    if "active" in values:
        values["active"] = 1 if values["active"] in (True, 1, "1", "true") else 0
    meta = data.get("meta")
    with db.connect() as c:
        existing = c.execute("SELECT id FROM facilities WHERE oid=?", (oid,)).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in values)
            params = list(values.values())
            extra = ", is_local=?" if is_local else ""
            if is_local:
                params.append(1)
            if meta is not None:
                extra += ", meta=?"
                params.append(jdump(meta))
            c.execute(f"UPDATE facilities SET {sets}{extra}, updated_at=? WHERE oid=?",
                      (*params, now, oid))
        else:
            cols = list(values) + ["id", "is_local", "meta", "created_at", "updated_at"]
            params = list(values.values()) + [new_id(), 1 if is_local else 0,
                                              jdump(meta or {}), now, now]
            c.execute(f"INSERT INTO facilities({', '.join(cols)}) "
                      f"VALUES ({', '.join('?' for _ in cols)})", params)
    return get_by_oid(oid)  # type: ignore[return-value]


def delete(oid: str) -> bool:
    with db.connect() as c:
        cur = c.execute("DELETE FROM facilities WHERE oid=? AND is_local=0", (oid,))
        return cur.rowcount > 0


def local() -> dict:
    """The local facility row, seeding it from the environment if needed."""
    f = get_by_oid(settings.facility_oid())
    return f if f else seed_local()


def seed_local() -> dict:
    base = settings.public_base_url()
    data: dict[str, Any] = {
        "oid": settings.facility_oid(),
        "name": settings.facility_name(),
        "kind": settings.facility_kind(),
        "ae_title": settings.env("PACS_AE_TITLE", "ARANMED"),
        "dicom_host": settings.env("PACS_DIMSE_PUBLIC_HOST", "") or None,
        "dicom_port": settings.env_int("PACS_DIMSE_PORT", 11112),
        "base_url": base or None,
        "fhir_base": (base + settings.env("FHIR_PREFIX", "/fhir")) if base else None,
        "dicomweb_base": (base + settings.env("DICOMWEB_PREFIX", "/dicom-web")) if base else None,
        "mllp_host": settings.env("HL7_MLLP_PUBLIC_HOST", "") or None,
        "mllp_port": settings.env_int("HL7_MLLP_PORT", 2575),
        "trust_level": "self",
        "active": True,
    }
    return upsert(data, is_local=True)


def seed_peers_from_file() -> int:
    """Load peers from ``FACILITY_PEERS_FILE`` (a JSON list), if configured."""
    path = settings.env("FACILITY_PEERS_FILE", "")
    if not path:
        return 0
    p = Path(path)
    if not p.is_file():
        log.warning("FACILITY_PEERS_FILE=%s does not exist", path)
        return 0
    try:
        entries = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        log.error("FACILITY_PEERS_FILE is not valid JSON: %s", e)
        return 0
    n = 0
    for entry in entries if isinstance(entries, list) else []:
        try:
            upsert(entry)
            n += 1
        except ValueError as e:
            log.warning("skipping peer entry: %s", e)
    return n


def peer_secret(facility: dict) -> Optional[str]:
    env_key = facility.get("secret_env")
    return os.environ.get(env_key) if env_key else None
