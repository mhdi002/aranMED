"""Application-level encryption for PHI at rest.

Disk encryption protects a stolen laptop; it does nothing once the machine is
running, a database file is copied out, or a backup lands somewhere it
shouldn't. Encrypting the patient payload itself narrows that: the SQLite
file, its WAL, and any backup of it carry ciphertext, and the key lives
outside the database.

Design:

* **AES-256-GCM**, via ``cryptography``. Authenticated encryption, so a
  tampered ciphertext fails loudly instead of decrypting to garbage. This is
  the one place the project takes a crypto dependency rather than staying
  stdlib-only — hand-rolling a cipher is the mistake this module exists to
  avoid, and stdlib has no AEAD.
* **Per-record random nonce**, never reused. GCM's security collapses under
  nonce reuse with the same key, so it is generated fresh on every write.
* **AAD binds the ciphertext to its row.** The patient id and owner id are
  authenticated (not encrypted), so a blob lifted from one patient's row and
  pasted into another's fails to decrypt instead of silently swapping records.
* **Key ids allow rotation.** Every blob records which key encrypted it, so a
  new key can be introduced while old rows are still readable. Re-encryption
  happens naturally on the next write, or in bulk via ``reencrypt_all``.
* **Opt-in and backward compatible.** With no key configured, values pass
  through unchanged and a warning is logged. Existing plaintext rows stay
  readable after a key is added, and get encrypted the next time they are
  written — no migration step is required to turn this on.

Configuration (see .env.example):

``PHI_ENCRYPTION_KEYS``
    ``keyid:base64key`` pairs, comma-separated. The **first** entry is the
    active key used for new writes; the rest are kept for decryption only.
``PHI_ENCRYPTION_KEY_FILE``
    Path to a file holding the same, for deployments that mount secrets as
    files rather than environment variables.

Generate a key::

    python -c "import base64,os;print('k1:'+base64.b64encode(os.urandom(32)).decode())"
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("phi_crypto")

# Marker prefix so an encrypted blob is unmistakable and we never try to
# JSON-parse ciphertext (or AES-decrypt legacy plaintext).
_MAGIC = "ARANMED-PHI-v1:"

_keys: dict[str, bytes] = {}
_active_key_id: Optional[str] = None
_loaded = False
_lock = threading.Lock()
_warned_no_key = False


def _parse_key_spec(spec: str) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            log.error("phi_crypto: ignoring malformed key entry (expected 'keyid:base64key')")
            continue
        key_id, b64 = chunk.split(":", 1)
        key_id = key_id.strip()
        try:
            raw = base64.b64decode(b64.strip(), validate=True)
        except Exception:  # noqa: BLE001
            log.error("phi_crypto: key %r is not valid base64 — ignoring", key_id)
            continue
        if len(raw) != 32:
            log.error("phi_crypto: key %r is %d bytes, expected 32 (AES-256) — ignoring",
                      key_id, len(raw))
            continue
        out.append((key_id, raw))
    return out


def _load_keys() -> None:
    global _loaded, _active_key_id
    with _lock:
        if _loaded:
            return
        spec = os.environ.get("PHI_ENCRYPTION_KEYS", "").strip()
        key_file = os.environ.get("PHI_ENCRYPTION_KEY_FILE", "").strip()
        if not spec and key_file:
            try:
                spec = Path(key_file).read_text(encoding="utf-8").strip()
            except OSError as e:
                log.error("phi_crypto: could not read PHI_ENCRYPTION_KEY_FILE (%s)", e)
        parsed = _parse_key_spec(spec) if spec else []
        for key_id, raw in parsed:
            _keys[key_id] = raw
        if parsed:
            _active_key_id = parsed[0][0]
            log.info("phi_crypto: encryption ENABLED (active key %r, %d key(s) loaded)",
                     _active_key_id, len(_keys))
        _loaded = True


def reload_keys() -> None:
    """Re-read key configuration (tests, key rotation without restart)."""
    global _loaded, _active_key_id, _warned_no_key
    with _lock:
        _keys.clear()
        _active_key_id = None
        _loaded = False
        _warned_no_key = False
    _load_keys()


def enabled() -> bool:
    _load_keys()
    return _active_key_id is not None


def active_key_id() -> Optional[str]:
    _load_keys()
    return _active_key_id


def _aesgcm(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415
    return AESGCM(key)


def _aad(patient_id: str, owner_user_id: int | None) -> bytes:
    """Authenticated-but-not-encrypted context binding a blob to its row."""
    return f"{patient_id}|{owner_user_id if owner_user_id is not None else ''}".encode("utf-8")


def encrypt_json(value: Any, *, patient_id: str, owner_user_id: int | None) -> str:
    """Serialise *value* and encrypt it. Returns plain JSON when no key is set.

    The return value is always a string safe to store in a TEXT column.
    """
    global _warned_no_key
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    _load_keys()
    if _active_key_id is None:
        if not _warned_no_key:
            log.warning(
                "phi_crypto: PHI_ENCRYPTION_KEYS is not set — patient data is being "
                "stored UNENCRYPTED. Set a key to enable encryption at rest."
            )
            _warned_no_key = True
        return payload

    nonce = os.urandom(12)  # 96-bit, the GCM-recommended size
    ct = _aesgcm(_keys[_active_key_id]).encrypt(
        nonce, payload.encode("utf-8"), _aad(patient_id, owner_user_id)
    )
    return (
        _MAGIC
        + _active_key_id
        + ":"
        + base64.b64encode(nonce).decode("ascii")
        + ":"
        + base64.b64encode(ct).decode("ascii")
    )


def decrypt_json(stored: str | None, *, patient_id: str,
                 owner_user_id: int | None) -> Any:
    """Inverse of :func:`encrypt_json`.

    Rows written before encryption was enabled are plain JSON and are
    returned as-is, so turning encryption on needs no migration.
    """
    if stored is None:
        return None
    if not stored.startswith(_MAGIC):
        # Legacy plaintext row.
        try:
            return json.loads(stored)
        except (ValueError, TypeError):
            return None

    _load_keys()
    body = stored[len(_MAGIC):]
    try:
        key_id, nonce_b64, ct_b64 = body.split(":", 2)
        nonce = base64.b64decode(nonce_b64)
        ct = base64.b64decode(ct_b64)
    except Exception as e:  # noqa: BLE001
        log.error("phi_crypto: malformed ciphertext for %s (%s)", patient_id, e)
        raise ValueError("stored PHI is malformed") from e

    key = _keys.get(key_id)
    if key is None:
        # Refuse rather than return nothing: silently yielding an empty record
        # would look like "this patient has no medications", which in a
        # clinical system is a dangerous thing to be wrong about.
        log.error("phi_crypto: no key %r available to decrypt %s", key_id, patient_id)
        raise ValueError(
            f"PHI for {patient_id} was encrypted with key '{key_id}', which is not "
            "configured — restore it in PHI_ENCRYPTION_KEYS"
        )

    try:
        pt = _aesgcm(key).decrypt(nonce, ct, _aad(patient_id, owner_user_id))
    except Exception as e:  # noqa: BLE001
        log.error("phi_crypto: authentication failed decrypting %s (%s)", patient_id, e)
        raise ValueError(
            f"PHI for {patient_id} failed authentication — the record or its "
            "owner/id binding has been altered"
        ) from e
    return json.loads(pt.decode("utf-8"))


def is_encrypted(stored: str | None) -> bool:
    return bool(stored) and stored.startswith(_MAGIC)


def reencrypt_all() -> dict[str, int]:
    """Rewrite every patient row under the active key.

    Use after adding a key (to encrypt legacy plaintext) or after rotating one
    (to retire the old key). Idempotent; rows already under the active key are
    skipped — except that the denormalised ``name`` column is cleared on every
    pass, because it is plaintext PHI in its own right and a row whose blob is
    already current can still be carrying a name left over from before
    encryption was switched on. Returns counts.
    """
    import db  # local import: db imports config, avoid a cycle at module load

    _load_keys()
    if _active_key_id is None:
        raise RuntimeError("no active PHI encryption key configured")

    stats = {"scanned": 0, "reencrypted": 0, "skipped": 0,
             "names_cleared": 0, "failed": 0}
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, owner_user_id, name, data FROM patients"
        ).fetchall()
        for r in rows:
            stats["scanned"] += 1
            stored = r["data"]

            # The denormalised name column is plaintext PHI. Clear it whether
            # or not the blob needs rewriting — the name is recoverable from
            # the decrypted record (see store.list_patients).
            if r["name"]:
                c.execute("UPDATE patients SET name='' WHERE id=?", (r["id"],))
                stats["names_cleared"] += 1

            if is_encrypted(stored) and stored[len(_MAGIC):].split(":", 1)[0] == _active_key_id:
                stats["skipped"] += 1
                continue
            try:
                value = decrypt_json(stored, patient_id=r["id"],
                                     owner_user_id=r["owner_user_id"])
                fresh = encrypt_json(value, patient_id=r["id"],
                                     owner_user_id=r["owner_user_id"])
                c.execute("UPDATE patients SET data=? WHERE id=?", (fresh, r["id"]))
                stats["reencrypted"] += 1
            except Exception as e:  # noqa: BLE001
                log.error("phi_crypto: could not re-encrypt %s: %s", r["id"], e)
                stats["failed"] += 1
    log.info("phi_crypto: reencrypt_all %s", stats)
    return stats
