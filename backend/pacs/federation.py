"""Federated imaging search across this archive, remote nodes and peer hospitals.

A doctor looking for a patient's imaging should not need to know which
hospital or which archive holds it. ``search`` fans a query out — in
parallel, each with its own timeout — to:

* the local index,
* every active ``pacs_nodes`` row flagged ``federate``,
* every active peer facility in the registry that publishes a DICOMweb
  base URL (queried with a signed peer token),

and merges results by StudyInstanceUID (globally unique), listing every
location that holds the study. A remote that is down degrades the answer
(reported in ``errors``) instead of failing it.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
from typing import Any, Optional

from clinicaldb import facilities, settings
from pacs import index, nodes
from pacs.adapters import AdapterError, for_node

log = logging.getLogger("pacs.federation")


def _facility_node(f: dict) -> dict:
    return {"id": f"facility:{f['oid']}", "name": f["name"], "kind": "dicomweb",
            "base_url": f["dicomweb_base"], "facility_oid": f["oid"],
            "issuer": f["oid"], "active": True, "federate": True}


def remote_targets(include_facilities: bool = True) -> list[dict]:
    out = [n for n in nodes.list_nodes(active_only=True) if n.get("federate")]
    if include_facilities:
        seen = {n.get("base_url") for n in out}
        for f in facilities.peers():
            if f.get("dicomweb_base") and f["dicomweb_base"] not in seen:
                out.append(_facility_node(f))
    return out


def resolve_target(target_id: str) -> Optional[dict]:
    if target_id.startswith("facility:"):
        f = facilities.get_by_oid(target_id.split(":", 1)[1])
        return _facility_node(f) if f and f.get("dicomweb_base") else None
    return nodes.get(target_id)


def search(filters: dict[str, Any], *, scope: str = "all", limit: int = 100,
           practitioner: Optional[str] = None, purpose: str = "TREAT") -> dict[str, Any]:
    """``scope`` is ``local``, ``remote`` or ``all``."""
    merged: dict[str, dict] = {}
    errors: list[dict] = []

    def add(item: dict, location: dict) -> None:
        uid = item.get("StudyInstanceUID")
        if not uid:
            return
        cur = merged.get(uid)
        if cur is None:
            cur = {**item, "locations": []}
            merged[uid] = cur
        cur["locations"].append(location)

    if scope in ("local", "all"):
        for s in index.query_studies(filters, limit=limit):
            add(s, {"kind": "local", "id": "local", "name": settings.facility_name(),
                    "facility_oid": settings.facility_oid()})

    if scope in ("remote", "all"):
        targets = remote_targets()
        timeout = settings.env_float("PACS_FEDERATION_TIMEOUT_SEC", 15.0)

        def run(node: dict) -> tuple[dict, list[dict]]:
            node = {**node, "_practitioner": practitioner or "system", "_purpose": purpose}
            return node, for_node(node).search_studies(filters, limit=limit)

        if targets:
            with cf.ThreadPoolExecutor(max_workers=min(8, len(targets))) as ex:
                futs = {ex.submit(run, n): n for n in targets}
                try:
                    for fut in cf.as_completed(futs, timeout=timeout):
                        node = futs[fut]
                        try:
                            _, rows = fut.result()
                        except (AdapterError, Exception) as e:  # noqa: BLE001
                            errors.append({"node": node.get("name"), "error": str(e)})
                            continue
                        for s in rows:
                            add(s, {"kind": node.get("kind"), "id": node.get("id"),
                                    "name": node.get("name"),
                                    "facility_oid": node.get("facility_oid")})
                except cf.TimeoutError:
                    for fut, node in futs.items():
                        if not fut.done():
                            errors.append({"node": node.get("name"), "error": "timed out"})
    studies = sorted(merged.values(), key=lambda s: (s.get("StudyDate") or "",
                                                    s.get("StudyTime") or ""), reverse=True)
    return {"studies": studies[:limit], "errors": errors}
