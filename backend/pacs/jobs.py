"""Background routing jobs: retrieve from / send to remote nodes, with retries.

Jobs are rows in ``pacs_jobs`` so their state survives a restart and is
visible in the UI; a small thread pool executes them. A failed attempt is
retried with exponential backoff up to ``PACS_JOB_MAX_ATTEMPTS``; jobs left
``running`` by a crash are re-queued at start-up.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import time
from typing import Any, Callable, Optional

import db
from clinicaldb import settings
from clinicaldb.util import jdump, jload, new_id, row
from pacs import config, schema  # noqa: F401

log = logging.getLogger("pacs.jobs")

_pool: Optional[cf.ThreadPoolExecutor] = None
_runners: dict[str, Callable[[dict], dict]] = {}


def register(kind: str, fn: Callable[[dict], dict]) -> None:
    _runners[kind] = fn


def _executor() -> cf.ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = cf.ThreadPoolExecutor(max_workers=settings.env_int("PACS_JOB_WORKERS", 2),
                                      thread_name_prefix="pacs-job")
    return _pool


def get(job_id: str) -> Optional[dict]:
    with db.connect() as c:
        j = row(c.execute("SELECT * FROM pacs_jobs WHERE id=?", (job_id,)).fetchone())
    if j:
        j["params"] = jload(j.get("params"), {})
        j["result"] = jload(j.get("result"))
    return j


def list_jobs(limit: int = 100, status: Optional[str] = None) -> list[dict]:
    sql, params = "SELECT id FROM pacs_jobs", []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    with db.connect() as c:
        ids = [r["id"] for r in c.execute(sql, (*params, limit)).fetchall()]
    return [get(i) for i in ids]


def _update(job_id: str, **fields: Any) -> None:
    if "result" in fields:
        fields["result"] = jdump(fields["result"])
    with db.connect() as c:
        c.execute(f"UPDATE pacs_jobs SET {', '.join(f'{k}=?' for k in fields)}, updated_at=? "
                  "WHERE id=?", (*fields.values(), db.now(), job_id))


def _execute(job_id: str) -> dict:
    job = get(job_id)
    runner = _runners.get(job["kind"])
    if runner is None:
        _update(job_id, status="failed", last_error=f"no runner for {job['kind']}")
        return get(job_id)
    delay = settings.env_float("PACS_JOB_RETRY_BASE_SEC", 1.0)
    while True:
        job = get(job_id)
        _update(job_id, status="running", attempts=job["attempts"] + 1)
        try:
            result = runner(job)
            _update(job_id, status="done", result=result, last_error=None)
            return get(job_id)
        except Exception as e:  # noqa: BLE001
            log.warning("job %s (%s) attempt %d failed: %s", job_id, job["kind"],
                        job["attempts"] + 1, e)
            if job["attempts"] + 1 >= job["max_attempts"]:
                _update(job_id, status="failed", last_error=str(e)[:2000])
                return get(job_id)
            _update(job_id, status="queued", last_error=str(e)[:2000])
            time.sleep(delay)
            delay *= 2


def submit(kind: str, *, target: Optional[str], study_uid: Optional[str] = None,
           params: Optional[dict] = None, created_by: Optional[str] = None,
           wait: bool = False) -> dict:
    job_id = new_id()
    now = db.now()
    with db.connect() as c:
        c.execute("INSERT INTO pacs_jobs(id, kind, status, target, study_uid, params, attempts, "
                  "max_attempts, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (job_id, kind, "queued", target, study_uid, jdump(params or {}), 0,
                   config.job_max_attempts(), created_by, now, now))
    if wait:
        return _execute(job_id)
    _executor().submit(_execute, job_id)
    return get(job_id)


def requeue_stale() -> int:
    with db.connect() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM pacs_jobs WHERE status IN ('running','queued')").fetchall()]
    for i in ids:
        _executor().submit(_execute, i)
    return len(ids)


def shutdown() -> None:
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


# --- built-in job kinds -------------------------------------------------------
def _retrieve(job: dict) -> dict:
    from pacs import federation
    from pacs.adapters import for_node
    node = federation.resolve_target(job["target"])
    if not node:
        raise RuntimeError(f"unknown target {job['target']}")
    p = job.get("params") or {}
    node = {**node, "_practitioner": p.get("practitioner") or "system",
            "_purpose": p.get("purpose") or "TREAT"}
    return for_node(node).retrieve_study(job["study_uid"], p.get("series_uid"))


def _send(job: dict) -> dict:
    from pacs import federation
    from pacs.adapters import for_node
    node = federation.resolve_target(job["target"])
    if not node:
        raise RuntimeError(f"unknown target {job['target']}")
    p = job.get("params") or {}
    node = {**node, "_practitioner": p.get("practitioner") or "system",
            "_purpose": p.get("purpose") or "TREAT"}
    return for_node(node).send_study(job["study_uid"])


register("retrieve", _retrieve)
register("send", _send)
