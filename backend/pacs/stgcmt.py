"""Storage Commitment Push Model (SCP side), durable.

A modality asks "do you now safely hold these instances?" with an N-ACTION
and deletes its local copies once the answer (an N-EVENT-REPORT) says yes,
so a report that is silently lost leaves the modality's disk filling up and
a report that wrongly says yes loses images. Each request is therefore a row
in ``pacs_stgcmt`` until its report has been delivered:

* ``stgcmt_mode`` on the node chooses how the report is delivered —
  ``new`` (default): a new association to the node's host/port, proposing
  the SCP role (Philips, Canon and most devices);
  ``same``: on the requesting association while it is still open (devices
  that cannot accept incoming associations);
  ``auto``: try the requesting association first, then a new one (Siemens
  waits a few seconds on the same association and then expects a new one).
* Delivery failures are retried with exponential backoff by a worker
  thread; rows still pending after a restart are picked up again.
* The report association uses the node's TLS settings.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

from pydicom.dataset import Dataset

import db
from clinicaldb import messages
from clinicaldb.util import row
from pacs import config, index, nodes, schema  # noqa: F401

log = logging.getLogger("pacs.stgcmt")

NO_SUCH_OBJECT = 0x0112
_EVENT_SUCCESS, _EVENT_FAILURES = 1, 2
_lock = threading.Lock()
_worker: Optional[threading.Thread] = None
_stop = threading.Event()


def _now() -> float:
    return time.time()


def record(transaction_uid: str, calling_ae: str, refs: list[tuple[str, str]]) -> dict:
    """Persist a commitment request. A re-sent transaction UID resets it."""
    now = _now()
    payload = json.dumps(refs)
    with db.connect() as c:
        if c.execute("SELECT 1 FROM pacs_stgcmt WHERE transaction_uid=?",
                     (transaction_uid,)).fetchone():
            c.execute("UPDATE pacs_stgcmt SET calling_ae=?, refs=?, status='pending', attempts=0, "
                      "last_error=NULL, next_attempt_at=?, updated_at=? WHERE transaction_uid=?",
                      (calling_ae, payload, now, now, transaction_uid))
        else:
            c.execute("INSERT INTO pacs_stgcmt(transaction_uid, calling_ae, refs, status, "
                      "next_attempt_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                      (transaction_uid, calling_ae, payload, "pending", now, now, now))
    return get(transaction_uid)  # type: ignore[return-value]


def get(transaction_uid: str) -> Optional[dict]:
    with db.connect() as c:
        r = row(c.execute("SELECT * FROM pacs_stgcmt WHERE transaction_uid=?",
                          (transaction_uid,)).fetchone())
    if r:
        r["refs"] = json.loads(r["refs"] or "[]")
    return r


def list_transactions(limit: int = 100, status: Optional[str] = None) -> list[dict]:
    sql, params = "SELECT * FROM pacs_stgcmt", []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    with db.connect() as c:
        rows = [row(r) for r in c.execute(sql + " ORDER BY created_at DESC LIMIT ?",
                                          (*params, limit)).fetchall()]
    for r in rows:
        r["refs"] = len(json.loads(r.pop("refs") or "[]"))
    return rows


def _claim(transaction_uid: str) -> bool:
    """Mark a pending row as being sent; False if someone else has it."""
    with db.connect() as c:
        return c.execute("UPDATE pacs_stgcmt SET status='sending', updated_at=? "
                         "WHERE transaction_uid=? AND status='pending'",
                         (_now(), transaction_uid)).rowcount == 1


def _finish(transaction_uid: str, committed: int, failed: int) -> None:
    with db.connect() as c:
        c.execute("UPDATE pacs_stgcmt SET status='reported', committed=?, failed=?, "
                  "attempts=attempts+1, last_error=NULL, updated_at=? WHERE transaction_uid=?",
                  (committed, failed, _now(), transaction_uid))


def _retry_later(transaction_uid: str, error: str, *, final: bool = False) -> None:
    with db.connect() as c:
        r = row(c.execute("SELECT attempts FROM pacs_stgcmt WHERE transaction_uid=?",
                          (transaction_uid,)).fetchone()) or {"attempts": 0}
        attempts = int(r["attempts"]) + 1
        give_up = final or attempts >= config.stgcmt_max_attempts()
        delay = min(300.0, 5.0 * (2 ** (attempts - 1)))
        c.execute("UPDATE pacs_stgcmt SET status=?, attempts=?, last_error=?, "
                  "next_attempt_at=?, updated_at=? WHERE transaction_uid=?",
                  ("failed" if give_up else "pending", attempts, error[:500],
                   _now() + delay, _now(), transaction_uid))
    log.warning("storage commitment %s: %s (%s)", transaction_uid, error,
                "giving up" if give_up else f"retry in {delay:.0f}s")


def build_report(transaction_uid: str, refs: list[tuple[str, str]]) -> tuple[Dataset, int, int, int]:
    """(event information, event type, committed count, failed count).
    An instance is committed only if it is indexed AND its file is present."""
    from pacs.storage import get_storage
    st = get_storage()
    held = {}
    if refs:
        for i in index.instance_paths(sop_uids=[r[1] for r in refs]):
            held[i["sop_uid"]] = i
    ok, bad = [], []
    for cls, inst in refs:
        meta = held.get(inst)
        if meta and meta.get("sop_class_uid") in (None, "", cls) and st.exists(meta["path"]):
            ok.append((cls, inst))
        else:
            bad.append((cls, inst))

    def items(pairs, failed=False):
        out = []
        for cls, inst in pairs:
            it = Dataset()
            it.ReferencedSOPClassUID = cls
            it.ReferencedSOPInstanceUID = inst
            if failed:
                it.FailureReason = NO_SUCH_OBJECT
            out.append(it)
        return out

    ds = Dataset()
    ds.TransactionUID = transaction_uid
    ds.RetrieveAETitle = config.ae_title()
    if ok:
        ds.ReferencedSOPSequence = items(ok)
    if bad:
        ds.FailedSOPSequence = items(bad, failed=True)
    return ds, (_EVENT_FAILURES if bad else _EVENT_SUCCESS), len(ok), len(bad)


def _send(assoc, transaction_uid: str, refs) -> tuple[int, int]:
    from pynetdicom.sop_class import StorageCommitmentPushModel
    ds, event_type, ok, bad = build_report(transaction_uid, refs)
    status, _ = assoc.send_n_event_report(ds, event_type, StorageCommitmentPushModel,
                                          "1.2.840.10008.1.20.1.1")
    code = getattr(status, "Status", None)
    if code is None or code not in (0x0000,):
        raise RuntimeError(f"N-EVENT-REPORT answered with status {code!r}")
    return ok, bad


def _deliver_new(txn: dict, node: dict) -> None:
    from pynetdicom import AE, build_role
    from pynetdicom.sop_class import StorageCommitmentPushModel
    ae = AE(ae_title=config.ae_title())
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = config.network_timeout()
    ae.add_requested_context(StorageCommitmentPushModel)
    role = build_role(StorageCommitmentPushModel, scp_role=True)
    ctx = nodes.tls_context(node)
    assoc = ae.associate(node["host"], int(node["port"]), ae_title=node["ae_title"],
                         ext_neg=[role], tls_args=(ctx, None) if ctx else None,
                         max_pdu=config.node_max_pdu(node))
    if not assoc.is_established:
        raise ConnectionError(f"could not associate with {node['ae_title']}@{node['host']}:{node['port']}")
    try:
        ok, bad = _send(assoc, txn["transaction_uid"], txn["refs"])
    finally:
        assoc.release()
    _finish(txn["transaction_uid"], ok, bad)
    messages.log(direction="out", protocol="dicom", message_type="N-EVENT-REPORT",
                 peer=node["ae_title"], status="ok",
                 payload=f"storage commitment {txn['transaction_uid']}: {ok} committed, {bad} failed")


def deliver(transaction_uid: str) -> bool:
    """Send one pending report on a new association. True when delivered."""
    if not _claim(transaction_uid):
        return False
    txn = get(transaction_uid)
    if not txn:
        return False
    node = nodes.by_ae(txn["calling_ae"])
    if not node or node.get("kind") != "dimse":
        _retry_later(transaction_uid, f"no DIMSE node registered for {txn['calling_ae']}")
        return False
    if (node.get("stgcmt_mode") or "new") == "same":
        _retry_later(transaction_uid, "requesting association closed before the report "
                     "could be sent (node accepts reports on the same association only)",
                     final=True)
        return False
    try:
        _deliver_new(txn, node)
        return True
    except Exception as e:  # noqa: BLE001
        _retry_later(transaction_uid, str(e) or e.__class__.__name__)
        return False


def on_request(event, transaction_uid: str, refs: list[tuple[str, str]], node: Optional[dict]) -> None:
    """Called from the N-ACTION handler (after it has decided to accept)."""
    calling = str(event.assoc.requestor.ae_title).strip()
    record(transaction_uid, calling, refs)
    mode = ((node or {}).get("stgcmt_mode") or "new").lower()
    threading.Thread(target=_first_attempt, args=(event.assoc, transaction_uid, mode),
                     daemon=True, name="pacs-stgcmt").start()


def _first_attempt(assoc, transaction_uid: str, mode: str) -> None:
    if mode in ("same", "auto"):
        # The N-ACTION response is sent after the handler returns; give it a
        # moment, then report on the same association if it is still open.
        deadline = _now() + config.stgcmt_same_assoc_sec()
        time.sleep(0.05)
        while _now() < deadline and assoc.is_established:
            if _claim(transaction_uid):
                txn = get(transaction_uid) or {}
                try:
                    ok, bad = _send(assoc, transaction_uid, txn.get("refs") or [])
                    _finish(transaction_uid, ok, bad)
                    messages.log(direction="out", protocol="dicom",
                                 message_type="N-EVENT-REPORT", peer=txn.get("calling_ae"),
                                 status="ok", payload=f"storage commitment {transaction_uid} "
                                 f"(same association): {ok} committed, {bad} failed")
                    return
                except Exception as e:  # noqa: BLE001
                    log.info("same-association report for %s failed: %s", transaction_uid, e)
                    with db.connect() as c:
                        c.execute("UPDATE pacs_stgcmt SET status='pending' WHERE "
                                  "transaction_uid=? AND status='sending'", (transaction_uid,))
                break
            time.sleep(0.05)
    deliver(transaction_uid)


def deliver_due() -> int:
    """Deliver every pending report whose retry time has come."""
    with db.connect() as c:
        due = [r["transaction_uid"] for r in c.execute(
            "SELECT transaction_uid FROM pacs_stgcmt WHERE status='pending' AND next_attempt_at<=? "
            "ORDER BY created_at LIMIT 50", (_now(),)).fetchall()]
    return sum(1 for t in due if deliver(t))


def _loop(interval: float) -> None:
    while not _stop.wait(interval):
        try:
            deliver_due()
        except Exception:  # noqa: BLE001
            log.exception("storage commitment worker failed")


def start_worker(interval: float = 5.0) -> None:
    """Start the retry worker. Rows left 'sending' by a crash are re-queued."""
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return
        with db.connect() as c:
            c.execute("UPDATE pacs_stgcmt SET status='pending' WHERE status='sending'")
        _stop.clear()
        _worker = threading.Thread(target=_loop, args=(interval,), daemon=True,
                                   name="pacs-stgcmt-worker")
        _worker.start()


def stop_worker() -> None:
    global _worker
    _stop.set()
    if _worker:
        _worker.join(timeout=2)
    _worker = None


def summary() -> dict[str, Any]:
    with db.connect() as c:
        counts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) AS n FROM pacs_stgcmt GROUP BY status").fetchall()}
    return {"counts": counts, "recent": list_transactions(20)}
