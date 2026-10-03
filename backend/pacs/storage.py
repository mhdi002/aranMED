"""DICOM object storage.

A deliberately boring layout: ``<root>/<StudyUID>/<SeriesUID>/<SOPUID>.dcm``.
Every object is a DICOM Part 10 file exactly as stored, so the tree can be
backed up, rsync'd or re-indexed by any DICOM tool without this software.

* **Integrity:** the SHA-256 of every file is recorded in the index and
  re-checked on :func:`verify`; silent bit-rot in a PACS is a diagnostic
  hazard, not just a storage problem.
* **Atomic writes:** files are written to a temp name and renamed, so a
  crash mid-write never leaves a truncated object under a valid name.
* **No path injection:** UIDs are validated against the DICOM UID grammar
  (digits and dots, max 64 chars) before they touch the filesystem.

The backend is pluggable through :class:`Storage`; the filesystem one is the
default and the only one shipped. An object-store backend would implement
the same four methods.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from pacs import config

_UID = re.compile(r"^[0-9]+(\.[0-9]+)*$")


def valid_uid(uid: Optional[str]) -> bool:
    return bool(uid) and len(uid) <= 64 and bool(_UID.match(uid))


class Storage:
    def put(self, study: str, series: str, sop: str, data: bytes) -> tuple[str, str, int]:
        raise NotImplementedError

    def get(self, rel_path: str) -> bytes:
        raise NotImplementedError

    def delete(self, rel_path: str) -> None:
        raise NotImplementedError

    def exists(self, rel_path: str) -> bool:
        raise NotImplementedError


class FileStorage(Storage):
    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        r = self._root or config.storage_dir()
        r.mkdir(parents=True, exist_ok=True)
        return r

    def _abs(self, rel_path: str) -> Path:
        p = (self.root / rel_path).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("path escapes storage root")
        return p

    def put(self, study: str, series: str, sop: str, data: bytes) -> tuple[str, str, int]:
        for uid in (study, series, sop):
            if not valid_uid(uid):
                raise ValueError(f"invalid DICOM UID: {uid!r}")
        rel = f"{study}/{series}/{sop}.dcm"
        dest = self._abs(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".incoming-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return rel, hashlib.sha256(data).hexdigest(), len(data)

    def get(self, rel_path: str) -> bytes:
        return self._abs(rel_path).read_bytes()

    def delete(self, rel_path: str) -> None:
        p = self._abs(rel_path)
        try:
            p.unlink()
        except FileNotFoundError:
            return
        # Remove now-empty series/study directories.
        for parent in (p.parent, p.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                break

    def exists(self, rel_path: str) -> bool:
        return self._abs(rel_path).is_file()


_storage: Storage = FileStorage()


def get_storage() -> Storage:
    return _storage


def set_storage(s: Storage) -> None:
    global _storage
    _storage = s


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
