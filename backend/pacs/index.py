"""PACS index: ingest DICOM objects and answer hierarchical queries.

One ingest path serves every entry point — DIMSE C-STORE, DICOMweb STOW-RS,
browser upload, remote retrieve, inter-hospital transfer — so an object is
indexed identically however it arrived.

One query engine serves both C-FIND and QIDO-RS: callers pass DICOM keyword
filters (``PatientName``, ``StudyDate``, ``ModalitiesInStudy`` …) with
standard DICOM matching — wildcards ``*``/``?``, date/time ranges
``A-B``/``A-``/``-B``, UID lists — and get back plain dicts keyed by DICOM
keyword that :mod:`pacs.dicomjson` turns into datasets or DICOM JSON.
"""
from __future__ import annotations

import io
import logging
from typing import Any, Callable, Iterable, Optional

import db
from clinicaldb import mpi, settings
from clinicaldb.util import new_id, norm_name, row
from pacs import config, schema  # noqa: F401  (schema registers tables)
from pacs.storage import get_storage, valid_uid

log = logging.getLogger("pacs.index")

_ingest_hooks: list[Callable[[dict, bool], None]] = []


def register_ingest_hook(fn: Callable[[dict, bool], None]) -> None:
    """``fn(study_row, is_new_study)`` after every successful ingest."""
    if fn not in _ingest_hooks:
        _ingest_hooks.append(fn)


class IngestError(ValueError):
    """The object cannot be stored (bad UIDs, not DICOM, policy rejection)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _s(ds, keyword: str) -> Optional[str]:
    v = ds.get(keyword) if hasattr(ds, "get") else None
    if v is None or v == "":
        return None
    if hasattr(v, "value"):
        v = v.value
    if isinstance(v, (list, tuple)) or v.__class__.__name__ == "MultiValue":
        v = "\\".join(str(x) for x in v)
    v = str(v).strip().replace("\x00", "")
    return v or None


def _i(ds, keyword: str) -> Optional[int]:
    v = _s(ds, keyword)
    try:
        return int(float(v.split("\\")[0])) if v else None
    except ValueError:
        return None


def to_part10(ds, file_meta=None) -> bytes:
    """Serialise a dataset as a DICOM Part 10 file (preamble + file meta)."""
    import pydicom
    from pydicom.dataset import FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian

    if file_meta is not None:
        ds.file_meta = file_meta
    if not getattr(ds, "file_meta", None) or "TransferSyntaxUID" not in ds.file_meta:
        fm = FileMetaDataset()
        fm.TransferSyntaxUID = (ImplicitVRLittleEndian if getattr(ds, "is_implicit_VR", False)
                                else ExplicitVRLittleEndian)
        ds.file_meta = fm
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds, enforce_file_format=True)
    return buf.getvalue()


def _read(data: bytes):
    import pydicom
    from pydicom.errors import InvalidDicomError
    try:
        return pydicom.dcmread(io.BytesIO(data), force=True, stop_before_pixels=True)
    except (InvalidDicomError, Exception) as e:  # noqa: BLE001
        raise IngestError(f"not a readable DICOM object: {e}") from e


def _issuer_system(issuer: Optional[str]) -> str:
    issuer = (issuer or "").strip() or config.default_issuer()
    if issuer.startswith(("urn:", "http://", "https://")):
        return issuer
    return f"urn:oid:{issuer}" if all(p.isdigit() for p in issuer.split(".")) else f"urn:aranmed:issuer:{issuer}"


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
def ingest(data, *, source: str = "api", file_meta=None,
           origin_facility: Optional[str] = None,
           issuer_hint: Optional[str] = None) -> dict[str, Any]:
    """Store one DICOM object and index it.

    *data* is Part 10 bytes or a pydicom Dataset (as C-STORE delivers it).
    Returns ``{status, study_uid, series_uid, sop_uid, person_id, new_study}``
    where status is ``stored``, ``duplicate`` or ``replaced``.
    """
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
        if raw[128:132] != b"DICM":
            # Raw dataset without a Part 10 header (some modalities/exports):
            # re-encode it, refusing anything that isn't a DICOM object at all.
            import pydicom
            try:
                full = pydicom.dcmread(io.BytesIO(raw), force=True)
                raw = to_part10(full)
            except Exception as e:  # noqa: BLE001
                raise IngestError(f"not a DICOM object: {e}") from e
        ds = _read(raw)
    else:
        ds = data
        try:
            raw = to_part10(ds, file_meta)
        except Exception as e:  # noqa: BLE001
            raise IngestError(f"dataset cannot be encoded: {e}") from e
        ds = _read(raw)

    study_uid = _s(ds, "StudyInstanceUID")
    series_uid = _s(ds, "SeriesInstanceUID")
    sop_uid = _s(ds, "SOPInstanceUID")
    for name, uid in (("StudyInstanceUID", study_uid), ("SeriesInstanceUID", series_uid),
                      ("SOPInstanceUID", sop_uid)):
        if not valid_uid(uid):
            raise IngestError(f"missing or invalid {name}: {uid!r}")

    storage = get_storage()
    with db.connect() as c:
        existing = row(c.execute("SELECT sop_uid, sha256, path FROM pacs_instances WHERE sop_uid=?",
                                 (sop_uid,)).fetchone())
    policy = config.duplicate_policy()
    from pacs.storage import sha256 as _sha
    if existing:
        if existing["sha256"] == _sha(raw) or policy == "keep":
            return {"status": "duplicate", "study_uid": study_uid, "series_uid": series_uid,
                    "sop_uid": sop_uid, "person_id": None, "new_study": False}
        if policy == "reject":
            raise IngestError(f"SOP instance {sop_uid} already stored with different content")

    rel, digest, size = storage.put(study_uid, series_uid, sop_uid, raw)

    # --- patient identity (only resolved once per study) --------------------
    with db.connect() as c:
        study = row(c.execute("SELECT * FROM pacs_studies WHERE study_uid=?",
                              (study_uid,)).fetchone())
    new_study = study is None
    person_id = study["person_id"] if study else None
    patient_id = _s(ds, "PatientID")
    issuer = _s(ds, "IssuerOfPatientID") or issuer_hint
    if person_id is None:
        demo = mpi.normalise_demographics({
            "name": _s(ds, "PatientName"), "birth_date": _s(ds, "PatientBirthDate"),
            "sex": _s(ds, "PatientSex")})
        idents = []
        if patient_id:
            idents.append({"system": _issuer_system(issuer), "value": patient_id, "type": "MR",
                           "facility_oid": (issuer or config.default_issuer())})
        if idents or demo:
            person_id = mpi.register_person(demo, idents,
                                            source_facility=origin_facility)["person_id"]

    now = db.now()
    modality = _s(ds, "Modality")
    with db.connect() as c:
        db.begin_immediate(c)
        try:
            c.execute(
                "INSERT INTO pacs_studies(study_uid, person_id, facility_oid, patient_id, issuer, "
                "patient_name, patient_name_norm, patient_birth_date, patient_sex, accession, "
                "study_id, study_date, study_time, description, referring_physician, source, "
                "origin_facility, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(study_uid) DO UPDATE SET updated_at=excluded.updated_at",
                (study_uid, person_id, settings.facility_oid(), patient_id, issuer,
                 _s(ds, "PatientName"), norm_name((_s(ds, "PatientName") or "").replace("^", " ")),
                 _s(ds, "PatientBirthDate"), _s(ds, "PatientSex"), _s(ds, "AccessionNumber"),
                 _s(ds, "StudyID"), _s(ds, "StudyDate"), _s(ds, "StudyTime"),
                 _s(ds, "StudyDescription"), _s(ds, "ReferringPhysicianName"), source,
                 origin_facility or settings.facility_oid(), now, now))
            c.execute(
                "INSERT INTO pacs_series(series_uid, study_uid, modality, series_number, "
                "description, body_part, laterality, station_name, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(series_uid) DO UPDATE SET updated_at=excluded.updated_at",
                (series_uid, study_uid, modality, _i(ds, "SeriesNumber"),
                 _s(ds, "SeriesDescription"), _s(ds, "BodyPartExamined"), _s(ds, "Laterality"),
                 _s(ds, "StationName"), now, now))
            c.execute("DELETE FROM pacs_instances WHERE sop_uid=?", (sop_uid,))
            c.execute(
                "INSERT INTO pacs_instances(sop_uid, series_uid, study_uid, sop_class_uid, "
                "instance_number, transfer_syntax, path, sha256, size_bytes, rows_, columns_, "
                "frames, bits_allocated, photometric, window_center, window_width, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sop_uid, series_uid, study_uid, _s(ds, "SOPClassUID"),
                 _i(ds, "InstanceNumber"),
                 str(ds.file_meta.TransferSyntaxUID) if getattr(ds, "file_meta", None) else None,
                 rel, digest, size, _i(ds, "Rows"), _i(ds, "Columns"),
                 _i(ds, "NumberOfFrames") or (1 if _i(ds, "Rows") else None),
                 _i(ds, "BitsAllocated"), _s(ds, "PhotometricInterpretation"),
                 _s(ds, "WindowCenter"), _s(ds, "WindowWidth"), now))
            _refresh_counts(c, study_uid, series_uid)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        study = row(c.execute("SELECT * FROM pacs_studies WHERE study_uid=?",
                              (study_uid,)).fetchone())

    for hook in _ingest_hooks:
        try:
            hook(study, new_study)
        except Exception:  # noqa: BLE001
            log.exception("ingest hook %s failed for %s", getattr(hook, "__name__", hook), study_uid)
    return {"status": "replaced" if existing else "stored", "study_uid": study_uid,
            "series_uid": series_uid, "sop_uid": sop_uid, "person_id": person_id,
            "new_study": new_study}


def _refresh_counts(c, study_uid: str, series_uid: Optional[str] = None) -> None:
    if series_uid:
        c.execute("UPDATE pacs_series SET num_instances=(SELECT COUNT(*) FROM pacs_instances "
                  "WHERE series_uid=?) WHERE series_uid=?", (series_uid, series_uid))
    mods = [r["modality"] for r in c.execute(
        "SELECT DISTINCT modality FROM pacs_series WHERE study_uid=? AND modality IS NOT NULL",
        (study_uid,)).fetchall()]
    parts = [r["body_part"] for r in c.execute(
        "SELECT DISTINCT body_part FROM pacs_series WHERE study_uid=? AND body_part IS NOT NULL",
        (study_uid,)).fetchall()]
    c.execute(
        "UPDATE pacs_studies SET "
        "num_series=(SELECT COUNT(*) FROM pacs_series WHERE study_uid=?), "
        "num_instances=(SELECT COUNT(*) FROM pacs_instances WHERE study_uid=?), "
        "size_bytes=(SELECT COALESCE(SUM(size_bytes),0) FROM pacs_instances WHERE study_uid=?), "
        "modalities=?, body_parts=? WHERE study_uid=?",
        (study_uid, study_uid, study_uid,
         ("|" + "|".join(sorted(mods)) + "|") if mods else None,
         ("|" + "|".join(sorted(parts)) + "|") if parts else None, study_uid))


def delete_study(study_uid: str) -> int:
    storage = get_storage()
    with db.connect() as c:
        paths = [r["path"] for r in c.execute(
            "SELECT path FROM pacs_instances WHERE study_uid=?", (study_uid,)).fetchall()]
        c.execute("DELETE FROM pacs_instances WHERE study_uid=?", (study_uid,))
        c.execute("DELETE FROM pacs_series WHERE study_uid=?", (study_uid,))
        c.execute("DELETE FROM pacs_study_reports WHERE study_uid=?", (study_uid,))
        c.execute("DELETE FROM pacs_studies WHERE study_uid=?", (study_uid,))
    for p in paths:
        storage.delete(p)
    return len(paths)


def set_study_status(study_uid: str, status: str) -> None:
    with db.connect() as c:
        c.execute("UPDATE pacs_studies SET status=?, updated_at=? WHERE study_uid=?",
                  (status, db.now(), study_uid))


# ---------------------------------------------------------------------------
# Query engine
# ---------------------------------------------------------------------------
# keyword -> (column, match kind)
STUDY_ATTRS: dict[str, tuple[str, str]] = {
    "PatientName": ("s.patient_name", "pn"),
    "PatientID": ("s.patient_id", "wild"),
    "IssuerOfPatientID": ("s.issuer", "exact"),
    "PatientBirthDate": ("s.patient_birth_date", "range"),
    "PatientSex": ("s.patient_sex", "exact"),
    "StudyInstanceUID": ("s.study_uid", "uid"),
    "AccessionNumber": ("s.accession", "wild"),
    "StudyID": ("s.study_id", "wild"),
    "StudyDate": ("s.study_date", "range"),
    "StudyTime": ("s.study_time", "range"),
    "StudyDescription": ("s.description", "wild"),
    "ReferringPhysicianName": ("s.referring_physician", "pn"),
    "ModalitiesInStudy": ("s.modalities", "multi"),
}
SERIES_ATTRS: dict[str, tuple[str, str]] = {
    "SeriesInstanceUID": ("se.series_uid", "uid"),
    "Modality": ("se.modality", "exact"),
    "SeriesNumber": ("se.series_number", "int"),
    "SeriesDescription": ("se.description", "wild"),
    "BodyPartExamined": ("se.body_part", "wild"),
}
INSTANCE_ATTRS: dict[str, tuple[str, str]] = {
    "SOPInstanceUID": ("i.sop_uid", "uid"),
    "SOPClassUID": ("i.sop_class_uid", "uid"),
    "InstanceNumber": ("i.instance_number", "int"),
}
# Non-DICOM extensions useful to the UI / agent (prefixed so they can't clash).
EXT_ATTRS: dict[str, tuple[str, str]] = {
    "x-person-id": ("s.person_id", "exact"),
    "x-status": ("s.status", "exact"),
    "x-origin-facility": ("s.origin_facility", "exact"),
}


def _wild(v: str) -> str:
    return v.replace("*", "%").replace("?", "_")


def _clause(col: str, kind: str, value: str, params: list) -> Optional[str]:
    value = str(value).strip()
    if value == "" or value == "*":
        return None
    if kind == "uid":
        uids = [u.strip() for u in value.replace("\\", ",").split(",") if u.strip()]
        params.extend(uids)
        return f"{col} IN ({', '.join('?' for _ in uids)})"
    if kind == "range" and "-" in value:
        lo, hi = value.split("-", 1)
        lo, hi = lo.strip().replace("-", ""), hi.strip().replace("-", "")
        parts = []
        if lo:
            parts.append(f"{col} >= ?")
            params.append(lo)
        if hi:
            parts.append(f"{col} <= ?")
            # A shorter upper time bound ("1200") must still include
            # "120000.123": "~" sorts after every digit.
            params.append(hi + ("~" if col.endswith("_time") else ""))
        return " AND ".join(parts) or None
    if kind == "multi":
        vals = [m.strip().upper() for m in value.replace(",", "\\").split("\\") if m.strip()]
        # Stored as "|CT|MR|". Not backslash-delimited like DICOM: Postgres
        # treats "\\" in LIKE patterns as an escape character.
        params.extend(f"%|{m}|%" for m in vals)
        return "(" + " OR ".join(f"{col} LIKE ?" for _ in vals) + ")"
    if kind == "int":
        try:
            params.append(int(value))
        except ValueError:
            return "1=0"
        return f"{col} = ?"
    if kind == "pn":
        if "*" in value or "?" in value:
            params.append(_wild(value).lower())
            return f"LOWER({col}) LIKE ?"
        # Exact PN match is case-insensitive; also accept "Family Given" order.
        params.append(value.lower())
        params.append("%" + norm_name(value.replace("^", " ")).replace(" ", "%") + "%")
        return f"(LOWER({col}) = ? OR s.patient_name_norm LIKE ?)" if col == "s.patient_name" \
            else f"(LOWER({col}) = ? OR LOWER({col}) LIKE ?)"
    if kind == "wild" and ("*" in value or "?" in value):
        params.append(_wild(value).lower())
        return f"LOWER({col}) LIKE ?"
    params.append(value.replace("-", "") if kind == "range" else value)
    return f"{col} = ?"


def _canon(filters: dict[str, Any]) -> dict[str, str]:
    """Accept keywords, hex tags ('00100010') and lower-case aliases."""
    from pydicom.datadict import keyword_for_tag
    out: dict[str, str] = {}
    for k, v in (filters or {}).items():
        if v is None:
            continue
        key = str(k)
        if len(key) == 8 and all(ch in "0123456789abcdefABCDEF" for ch in key):
            key = keyword_for_tag(int(key, 16)) or key
        out[key] = v if isinstance(v, str) else ",".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)
    return out


def _where(filters: dict[str, str], maps: Iterable[dict]) -> tuple[str, list]:
    params: list = []
    clauses: list[str] = []
    for m in maps:
        for kw, (col, kind) in m.items():
            if kw in filters:
                cl = _clause(col, kind, filters[kw], params)
                if cl:
                    clauses.append(cl)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _study_out(r: dict) -> dict[str, Any]:
    mods = [m for m in (r.get("modalities") or "").split("|") if m]
    return {
        "StudyInstanceUID": r["study_uid"], "PatientName": r.get("patient_name"),
        "PatientID": r.get("patient_id"), "IssuerOfPatientID": r.get("issuer"),
        "PatientBirthDate": r.get("patient_birth_date"), "PatientSex": r.get("patient_sex"),
        "AccessionNumber": r.get("accession"), "StudyID": r.get("study_id"),
        "StudyDate": r.get("study_date"), "StudyTime": r.get("study_time"),
        "StudyDescription": r.get("description"),
        "ReferringPhysicianName": r.get("referring_physician"),
        "ModalitiesInStudy": mods, "NumberOfStudyRelatedSeries": r.get("num_series"),
        "NumberOfStudyRelatedInstances": r.get("num_instances"),
        "_ext": {"person_id": r.get("person_id"), "status": r.get("status"),
                 "size_bytes": r.get("size_bytes"), "origin_facility": r.get("origin_facility"),
                 "body_parts": [b for b in (r.get("body_parts") or "").split("|") if b],
                 "priority": r.get("priority"), "source": r.get("source"),
                 "updated_at": r.get("updated_at")},
    }


def query_studies(filters: dict[str, Any] | None = None, *, limit: int = 100,
                  offset: int = 0) -> list[dict[str, Any]]:
    f = _canon(filters or {})
    where, params = _where(f, (STUDY_ATTRS, EXT_ATTRS))
    joins = ""
    # Series-level keys at study level (e.g. Modality) filter via EXISTS.
    se_where, se_params = _where(f, (SERIES_ATTRS,))
    if se_where:
        joins_clause = f"EXISTS (SELECT 1 FROM pacs_series se {se_where} AND se.study_uid=s.study_uid)"
        where = (where + " AND " if where else " WHERE ") + joins_clause
        params += se_params
    sql = (f"SELECT s.* FROM pacs_studies s{joins}{where} "
           "ORDER BY s.study_date DESC, s.study_time DESC, s.updated_at DESC LIMIT ? OFFSET ?")
    with db.connect() as c:
        rows = c.execute(sql, (*params, max(1, min(limit, 5000)), max(0, offset))).fetchall()
    return [_study_out(dict(r)) for r in rows]


def count_studies(filters: dict[str, Any] | None = None) -> int:
    f = _canon(filters or {})
    where, params = _where(f, (STUDY_ATTRS, EXT_ATTRS))
    with db.connect() as c:
        return int(c.execute(f"SELECT COUNT(*) AS n FROM pacs_studies s{where}",
                             params).fetchone()["n"])


def query_series(filters: dict[str, Any] | None = None, *, limit: int = 1000,
                 offset: int = 0) -> list[dict[str, Any]]:
    f = _canon(filters or {})
    where, params = _where(f, (STUDY_ATTRS, SERIES_ATTRS, EXT_ATTRS))
    sql = ("SELECT se.*, s.patient_name, s.patient_id, s.accession, s.study_date "
           "FROM pacs_series se JOIN pacs_studies s ON s.study_uid=se.study_uid"
           f"{where} ORDER BY se.series_number, se.created_at LIMIT ? OFFSET ?")
    with db.connect() as c:
        rows = c.execute(sql, (*params, limit, offset)).fetchall()
    return [{"StudyInstanceUID": r["study_uid"], "SeriesInstanceUID": r["series_uid"],
             "Modality": r["modality"], "SeriesNumber": r["series_number"],
             "SeriesDescription": r["description"], "BodyPartExamined": r["body_part"],
             "Laterality": r["laterality"],
             "NumberOfSeriesRelatedInstances": r["num_instances"],
             "PatientName": r["patient_name"], "PatientID": r["patient_id"],
             "AccessionNumber": r["accession"], "StudyDate": r["study_date"]}
            for r in rows]


def query_instances(filters: dict[str, Any] | None = None, *, limit: int = 10000,
                    offset: int = 0) -> list[dict[str, Any]]:
    f = _canon(filters or {})
    where, params = _where(f, (STUDY_ATTRS, SERIES_ATTRS, INSTANCE_ATTRS, EXT_ATTRS))
    sql = ("SELECT i.*, se.modality, se.series_number, s.patient_name, s.patient_id "
           "FROM pacs_instances i JOIN pacs_series se ON se.series_uid=i.series_uid "
           "JOIN pacs_studies s ON s.study_uid=i.study_uid"
           f"{where} ORDER BY se.series_number, i.instance_number, i.created_at LIMIT ? OFFSET ?")
    with db.connect() as c:
        rows = c.execute(sql, (*params, limit, offset)).fetchall()
    return [{"StudyInstanceUID": r["study_uid"], "SeriesInstanceUID": r["series_uid"],
             "SOPInstanceUID": r["sop_uid"], "SOPClassUID": r["sop_class_uid"],
             "InstanceNumber": r["instance_number"], "Rows": r["rows_"],
             "Columns": r["columns_"], "NumberOfFrames": r["frames"],
             "Modality": r["modality"], "PatientName": r["patient_name"],
             "PatientID": r["patient_id"],
             "_ext": {"path": r["path"], "transfer_syntax": r["transfer_syntax"],
                      "size_bytes": r["size_bytes"], "sha256": r["sha256"]}}
            for r in rows]


def query_patients(filters: dict[str, Any] | None = None, *, limit: int = 500) -> list[dict]:
    """Patient-level C-FIND: one row per (PatientID, issuer) with study counts."""
    f = _canon(filters or {})
    where, params = _where(f, ({k: STUDY_ATTRS[k] for k in
                                ("PatientName", "PatientID", "IssuerOfPatientID",
                                 "PatientBirthDate", "PatientSex")},))
    sql = ("SELECT s.patient_id, s.issuer, MAX(s.patient_name) AS patient_name, "
           "MAX(s.patient_birth_date) AS patient_birth_date, MAX(s.patient_sex) AS patient_sex, "
           "COUNT(*) AS n FROM pacs_studies s" + where +
           " GROUP BY s.patient_id, s.issuer ORDER BY patient_name LIMIT ?")
    with db.connect() as c:
        rows = c.execute(sql, (*params, limit)).fetchall()
    return [{"PatientID": r["patient_id"], "IssuerOfPatientID": r["issuer"],
             "PatientName": r["patient_name"], "PatientBirthDate": r["patient_birth_date"],
             "PatientSex": r["patient_sex"], "NumberOfPatientRelatedStudies": r["n"]}
            for r in rows]


def get_study(study_uid: str) -> Optional[dict]:
    res = query_studies({"StudyInstanceUID": study_uid}, limit=1)
    return res[0] if res else None


def instance_paths(*, study_uid: Optional[str] = None, series_uid: Optional[str] = None,
                   sop_uids: Optional[list[str]] = None) -> list[dict]:
    clauses, params = [], []
    if study_uid:
        clauses.append("study_uid=?")
        params.append(study_uid)
    if series_uid:
        clauses.append("series_uid=?")
        params.append(series_uid)
    if sop_uids:
        clauses.append(f"sop_uid IN ({', '.join('?' for _ in sop_uids)})")
        params.extend(sop_uids)
    if not clauses:
        return []
    with db.connect() as c:
        return [row(r) for r in c.execute(
            "SELECT * FROM pacs_instances WHERE " + " AND ".join(clauses)
            + " ORDER BY series_uid, instance_number", params).fetchall()]


def read_instance(sop_uid: str) -> Optional[bytes]:
    with db.connect() as c:
        r = c.execute("SELECT path FROM pacs_instances WHERE sop_uid=?", (sop_uid,)).fetchone()
    return get_storage().get(r["path"]) if r else None


def verify_study(study_uid: str) -> dict[str, Any]:
    """Re-hash every stored object of a study against the index."""
    from pacs.storage import sha256
    ok, bad, missing = 0, [], []
    for inst in instance_paths(study_uid=study_uid):
        try:
            data = get_storage().get(inst["path"])
        except FileNotFoundError:
            missing.append(inst["sop_uid"])
            continue
        if sha256(data) == inst["sha256"]:
            ok += 1
        else:
            bad.append(inst["sop_uid"])
    return {"ok": ok, "corrupt": bad, "missing": missing}


def stats() -> dict[str, Any]:
    with db.connect() as c:
        s = c.execute("SELECT COUNT(*) AS studies, COALESCE(SUM(num_instances),0) AS instances, "
                      "COALESCE(SUM(size_bytes),0) AS bytes FROM pacs_studies").fetchone()
        by_mod = c.execute("SELECT modality, COUNT(DISTINCT study_uid) AS n FROM pacs_series "
                           "GROUP BY modality ORDER BY n DESC").fetchall()
        by_status = c.execute("SELECT status, COUNT(*) AS n FROM pacs_studies GROUP BY status"
                              ).fetchall()
    return {"studies": int(s["studies"]), "instances": int(s["instances"]),
            "bytes": int(s["bytes"]),
            "by_modality": {r["modality"] or "?": int(r["n"]) for r in by_mod},
            "by_status": {r["status"]: int(r["n"]) for r in by_status}}


# Repoint PACS rows when the MPI merges two people.
def _on_merge(conn, survivor: str, merged: str) -> None:
    conn.execute("UPDATE pacs_studies SET person_id=? WHERE person_id=?", (survivor, merged))
    conn.execute("UPDATE pacs_worklist SET person_id=? WHERE person_id=?", (survivor, merged))
    conn.execute("UPDATE pacs_study_reports SET person_id=? WHERE person_id=?", (survivor, merged))


mpi.register_merge_hook(_on_merge)
