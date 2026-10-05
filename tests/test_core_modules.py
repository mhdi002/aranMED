"""Unit tests for core modules that previously had none: the login throttle
and one-time store, TOTP, the Postgres SQL dialect, template selection,
report clean-up, single-file DICOM ingest, bounded uploads and the
request-id / readiness / metrics plumbing."""
from __future__ import annotations

import asyncio
import io

import pytest


# ---------------------------------------------------------------- throttle
@pytest.fixture(params=["memory", "sqlite"])
def throttle_backend(request):
    import throttle
    b = throttle.MemoryThrottle() if request.param == "memory" else throttle.SqliteThrottle()
    throttle.set_backend(b)
    yield throttle
    throttle.set_backend(None)


def test_throttle_locks_after_the_limit_and_success_clears(throttle_backend):
    t = throttle_backend
    key = t.make_key("Alice ", "10.0.0.1")
    assert key == "alice|10.0.0.1"
    for _ in range(t.MAX_ATTEMPTS - 1):
        t.record_failure(key)
        assert t.seconds_locked(key) == 0
    t.record_failure(key)
    assert t.seconds_locked(key) > 0
    t.record_success(key)
    assert t.seconds_locked(key) == 0


def test_throttle_custom_limit(throttle_backend):
    t = throttle_backend
    key = t.ip_key("10.9.9.9")
    t.record_failure(key, 2)
    assert t.seconds_locked(key) == 0
    t.record_failure(key, 2)
    assert t.seconds_locked(key) > 0


def test_claim_once_is_single_use_until_expiry(throttle_backend):
    t = throttle_backend
    assert t.claim_once("value-1", 60)
    assert not t.claim_once("value-1", 60)
    assert t.claim_once("value-2", 60)
    assert t.claim_once("expiring", 0.01)
    import time
    time.sleep(0.05)
    assert t.claim_once("expiring", 60)


# ---------------------------------------------------------------- TOTP
def test_totp_matches_rfc6238_reference_vector():
    import base64
    import mfa
    # RFC 6238 appendix B, SHA-1 secret "12345678901234567890", T=59 -> 94287082 (8 digits)
    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    old = mfa.TOTP_DIGITS
    try:
        mfa.TOTP_DIGITS = 8
        assert mfa.code_at(secret, 59) == "94287082"
        assert mfa.code_at(secret, 1111111109) == "07081804"
    finally:
        mfa.TOTP_DIGITS = old


def test_totp_rejects_malformed_and_out_of_window_codes():
    import time
    import mfa
    secret = mfa.generate_secret()
    assert not mfa.verify(secret, "")
    assert not mfa.verify(secret, "abcdef")
    assert not mfa.verify(secret, "12345")
    stale = mfa.code_at(secret, time.time() - 10 * mfa.TOTP_STEP_SEC)
    assert not mfa.verify(secret, stale) or stale == mfa.code_at(secret)
    assert mfa.verify(secret, mfa.code_at(secret))
    uri = mfa.provisioning_uri(secret, account="dr x")
    assert uri.startswith("otpauth://totp/") and f"secret={secret}" in uri and "dr%20x" in uri


# ---------------------------------------------------------------- SQL dialect
def test_placeholders_translate_outside_literals_only():
    import dialect
    assert dialect.translate_placeholders("SELECT * FROM t WHERE a=? AND b=?") == \
        "SELECT * FROM t WHERE a=%s AND b=%s"
    assert dialect.translate_placeholders("SELECT '?' , \"x?\" FROM t WHERE c LIKE 'a%' AND d=?") == \
        "SELECT '?' , \"x?\" FROM t WHERE c LIKE 'a%%' AND d=%s"
    assert dialect.translate_placeholders("SELECT 'it''s ?' WHERE x=?") == "SELECT 'it''s ?' WHERE x=%s"


def test_sqlite_only_statements_are_rewritten():
    import dialect
    assert dialect.rewrite("PRAGMA journal_mode=WAL") is None
    assert dialect.rewrite("BEGIN IMMEDIATE") == "BEGIN"
    assert dialect.rewrite("INSERT OR IGNORE INTO t (a) VALUES (?)") == \
        "INSERT INTO t (a) VALUES (?) ON CONFLICT DO NOTHING"
    kept = "INSERT OR IGNORE INTO t (a) VALUES (?) ON CONFLICT (a) DO NOTHING"
    assert dialect.rewrite(kept) == "INSERT INTO t (a) VALUES (?) ON CONFLICT (a) DO NOTHING"
    assert dialect.rewrite("SELECT 1") == "SELECT 1"


# ---------------------------------------------------------------- templates
def test_template_selection_prefers_the_dictated_exam():
    import template_selection
    assert template_selection.suggest_templates("") == []
    out = template_selection.suggest_templates("thyroid sonography, right lobe nodule", top_k=3)
    assert out and out[0].template_id == "thyroid"
    assert 0 < out[0].confidence <= 1
    assert template_selection.auto_select_template("thyroid sonography") is not None
    assert template_selection.auto_select_template("hello there, nice weather") is None


# ---------------------------------------------------------------- report clean-up
def test_report_postprocess_drops_unfilled_residue_only():
    import report_postprocess
    report = ("FINDINGS:\nThe right lobe measures XX x XX mm.\n\n"
              "Normal echotexture.\nIMPRESSION:\nNo focal lesion.")
    out, dropped = report_postprocess.clean(report)
    assert "XX" not in out and "Normal echotexture." in out and "No focal lesion." in out
    assert dropped
    same, none = report_postprocess.clean("FINDINGS:\nNormal study.")
    assert same == "FINDINGS:\nNormal study." and none == []


def test_report_postprocess_can_be_disabled(monkeypatch):
    import report_postprocess
    monkeypatch.setenv("REPORT_POSTPROCESS", "false")
    text = "Size XX mm."
    assert report_postprocess.clean(text) == (text, [])


# ---------------------------------------------------------------- DICOM ingest
def test_single_file_dicom_ingest_reads_context_and_renders():
    import dicom_ingest
    from tests.functional.dicom_factory import make_study, to_bytes
    if not dicom_ingest.available():
        pytest.skip("pydicom not installed")
    _, ds = make_study(n_series=1, per_series=1, patient_name="Rahimi^Ali", patient_id="P-9",
                       modality="CT", study_desc="CHEST CT")
    raw = to_bytes(ds[0])
    meta = dicom_ingest.read_metadata(raw)
    assert meta["modality"] == "CT" and meta["has_pixel_data"]
    png = dicom_ingest.render_png(raw)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    patient = dicom_ingest.to_ehr_patient(meta)
    assert "Rahimi" in (patient.get("name") or "")
    assert not dicom_ingest.read_metadata(b"not a dicom file").get("has_pixel_data")


# ---------------------------------------------------------------- bounded uploads
def test_bounded_upload_reads_stop_at_the_limit():
    from fastapi import HTTPException, UploadFile
    import upload_limits

    def up(n):
        return UploadFile(file=io.BytesIO(b"x" * n), filename="f")
    assert upload_limits.read(up(10), 10) == b"x" * 10
    with pytest.raises(HTTPException) as e:
        upload_limits.read(up(11), 10)
    assert e.value.status_code == 413
    assert asyncio.run(upload_limits.aread(up(5), 10)) == b"x" * 5
    with pytest.raises(HTTPException):
        asyncio.run(upload_limits.aread(up(3 * 1024 * 1024), 1024 * 1024))


def test_upload_limit_answers_413_over_http(http_client, auth_headers, monkeypatch):
    monkeypatch.setenv("MAX_AUDIO_MB", "1")
    big = b"\0" * (1024 * 1024 + 10)
    r = http_client.post("/api/transcribe", headers=auth_headers,
                         files={"file": ("big.wav", big, "audio/wav")})
    assert r.status_code == 413


# ---------------------------------------------------------------- plumbing
def test_request_ids_are_echoed_or_minted(http_client):
    r = http_client.get("/api/live", headers={"X-Request-ID": "trace-123456"})
    assert r.headers["X-Request-ID"] == "trace-123456"
    r = http_client.get("/api/live", headers={"X-Request-ID": "bad id\nwith newline"})
    assert r.headers["X-Request-ID"] != "bad id\nwith newline" and len(r.headers["X-Request-ID"]) >= 6


def test_readiness_and_metrics(http_client, monkeypatch):
    assert http_client.get("/api/ready").json()["ok"] is True
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    assert http_client.get("/api/metrics").status_code == 404
    monkeypatch.setenv("METRICS_TOKEN", "scrape-secret")
    assert http_client.get("/api/metrics").status_code == 401
    r = http_client.get("/api/metrics", headers={"Authorization": "Bearer scrape-secret"})
    assert r.status_code == 200 and "aranmed_http_requests_total" in r.text
    assert 'route="/api/ready"' in r.text


def test_api_docs_can_be_switched_off(monkeypatch):
    import importlib
    import app as app_mod
    monkeypatch.setenv("DOCS_ENABLED", "false")
    importlib.reload(app_mod)
    from fastapi.testclient import TestClient
    assert TestClient(app_mod.app).get("/api/docs").status_code == 404
    monkeypatch.setenv("DOCS_ENABLED", "true")
    importlib.reload(app_mod)
    assert TestClient(app_mod.app).get("/api/openapi.json").status_code == 200


def test_fhir_paging_rejects_non_numbers(http_client):
    import auth
    auth.create_user(username="fhir_pager", password="pager-pass-1", role="admin")
    tok = http_client.post("/api/auth/login",
                           data={"username": "fhir_pager", "password": "pager-pass-1"}).json()["access_token"]
    r = http_client.get("/api/fhir/r4/Condition", params={"_count": "lots"},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 400
