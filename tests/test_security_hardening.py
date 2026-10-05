"""Regression tests for the round-4 security fixes.

Each test pins one hole that was open before: anonymous AI endpoints, template
path traversal, MFA re-enrolment without proof, conversations readable by
anyone holding the session id, peer tokens with unbounded lifetime, partial
FHIR transaction rollback, account management, and the MLLP exposure check.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(fresh_registry):
    import importlib
    import app as app_mod
    importlib.reload(app_mod)
    app_mod.registry = fresh_registry
    from agent import Agent
    app_mod.agent = Agent(registry=fresh_registry, memory=app_mod.memory)
    with TestClient(app_mod.app) as c:
        yield c


def _login(c: TestClient, username: str, password: str = "secure-pass-1", role: str = "doctor",
           **form) -> dict:
    import auth
    if not any(u["username"] == username for u in auth.list_users()):
        auth.create_user(username=username, password=password, role=role)
    r = c.post("/api/auth/login", data={"username": username, "password": password, **form})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ---------------------------------------------------------------- AI endpoints
def test_ai_endpoints_require_a_session(client, wav_bytes, monkeypatch):
    monkeypatch.delenv("ALLOW_ANONYMOUS_AI", raising=False)
    calls = [
        ("/api/transcribe", {"files": {"file": ("d.wav", wav_bytes, "audio/wav")}}),
        ("/api/dictate", {"files": {"file": ("d.wav", wav_bytes, "audio/wav")}}),
        ("/api/report", {"json": {"transcript": "x", "template_id": "chest"}}),
        ("/api/templates/suggest", {"json": {"transcript": "chest ultrasound"}}),
        ("/api/chat", {"data": {"text": "hello"}}),
        ("/api/vision", {"files": {"image": ("i.png", b"\x89PNG", "image/png")}}),
        ("/api/sessions/abc/reset", {}),
    ]
    for path, kw in calls:
        r = client.post(path, **kw)
        assert r.status_code == 401, (path, r.status_code, r.text)


def test_anonymous_opt_in_never_reaches_patient_records(client, fake_core, monkeypatch):
    monkeypatch.setenv("ALLOW_ANONYMOUS_AI", "true")
    fake_core.script = [("tool", "list_ehr", {}), ("answer", "done")]
    r = client.post("/api/chat", data={"text": "list the EHR records"})
    assert r.status_code == 200, r.text
    calls = r.json()["tool_calls"]
    assert calls and calls[0]["name"] == "list_ehr"
    assert "signed-in" in (calls[0]["result"].get("error") or ""), calls


# ---------------------------------------------------------------- templates
def test_template_ids_cannot_leave_the_templates_directory(client):
    import templates as templates_mod
    probe = templates_mod.TEMPLATES_DIR.parent / "zz_traversal_probe.txt"
    probe.write_text("TOP SECRET", encoding="utf-8")
    try:
        for tid in ("../zz_traversal_probe", "..%2Fzz_traversal_probe", "/etc/passwd", "a/../../x"):
            with pytest.raises(FileNotFoundError):
                templates_mod.get_template(tid)
        h = _login(client, "tpl_user")
        r = client.post("/api/report", headers=h,
                        json={"transcript": "x", "template_id": "../zz_traversal_probe"})
        assert r.status_code == 404
        assert "TOP SECRET" not in r.text
    finally:
        probe.unlink(missing_ok=True)
    # Ordinary ids still work.
    assert templates_mod.get_template("chest").strip()


# ---------------------------------------------------------------- MFA
def test_mfa_reenrol_needs_a_current_code(client):
    import mfa
    h = _login(client, "mfa_user")
    secret = client.post("/api/auth/mfa/enroll", headers=h).json()["secret"]
    r = client.post("/api/auth/mfa/verify", headers=h, json={"code": mfa.code_at(secret)})
    assert r.status_code == 200 and r.json()["mfa_enabled"]
    recovery = r.json()["recovery_codes"]

    # A stolen token alone can no longer swap out the second factor.
    r = client.post("/api/auth/mfa/enroll", headers=h)
    assert r.status_code == 409
    r = client.post("/api/auth/mfa/enroll", headers=h, json={"code": "000000"})
    assert r.status_code == 409
    me = client.get("/api/auth/me", headers=h).json()
    assert me["mfa_enabled"] is True
    # With proof (a recovery code) it is allowed.
    r = client.post("/api/auth/mfa/enroll", headers=h, json={"code": recovery[0]})
    assert r.status_code == 200 and r.json()["secret"] != secret


def test_recovery_codes_survive_a_secret_change(client, monkeypatch):
    import auth
    user = auth.create_user(username="rc_user", password="secure-pass-1", role="doctor")
    codes = auth.generate_recovery_codes(user["id"])
    monkeypatch.setattr(auth, "SECRET", "a-completely-different-secret")
    assert auth.consume_recovery_code(user["id"], codes[0])
    assert not auth.consume_recovery_code(user["id"], codes[0])  # single use


def test_totp_codes_cannot_be_replayed():
    import mfa
    secret = mfa.generate_secret()
    code = mfa.code_at(secret)
    assert mfa.verify(secret, code, user_id=4242)
    assert not mfa.verify(secret, code, user_id=4242)


# ---------------------------------------------------------------- conversations
def test_conversations_belong_to_their_owner(client, fake_core):
    fake_core.script = [("answer", "hello alice")]
    alice = _login(client, "alice_s")
    bob = _login(client, "bob_s")
    r = client.post("/api/chat", headers=alice, data={"text": "hi", "session_id": "shared-id"})
    assert r.status_code == 200, r.text
    r = client.post("/api/chat", headers=bob, data={"text": "what did she say?",
                                                   "session_id": "shared-id"})
    assert r.status_code == 403
    assert client.post("/api/sessions/shared-id/reset", headers=bob).status_code == 403
    assert client.post("/api/sessions/shared-id/reset", headers=alice).status_code == 200


# ---------------------------------------------------------------- accounts
def test_password_change_and_admin_account_controls(client):
    import auth
    admin = _login(client, "boss", role="admin")
    user_h = _login(client, "worker")
    worker = next(u for u in auth.list_users() if u["username"] == "worker")

    # Change own password: old sessions end, the response carries a fresh token.
    r = client.post("/api/auth/password", headers=user_h,
                    json={"current_password": "wrong", "new_password": "another-pass-22"})
    assert r.status_code == 400
    time.sleep(1.1)  # tokens carry whole-second iat
    r = client.post("/api/auth/password", headers=user_h,
                    json={"current_password": "secure-pass-1", "new_password": "another-pass-22"})
    assert r.status_code == 200, r.text
    fresh = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/auth/me", headers=user_h).status_code == 401
    assert client.get("/api/auth/me", headers=fresh).status_code == 200

    # Admin disables the account: its sessions stop working and sign-in fails.
    r = client.patch(f"/api/auth/users/{worker['id']}", headers=admin, json={"disabled": True})
    assert r.status_code == 200 and r.json()["user"]["disabled"] is True
    assert client.get("/api/auth/me", headers=fresh).status_code == 401
    r = client.post("/api/auth/login", data={"username": "worker", "password": "another-pass-22"})
    assert r.status_code == 401
    client.patch(f"/api/auth/users/{worker['id']}", headers=admin, json={"disabled": False})

    # Admin reset gives a one-time password that works.
    r = client.post(f"/api/auth/users/{worker['id']}/reset-password", headers=admin)
    temp = r.json()["temporary_password"]
    assert client.post("/api/auth/login",
                       data={"username": "worker", "password": temp}).status_code == 200

    # Role changes are validated; the last administrator cannot be removed.
    assert client.patch(f"/api/auth/users/{worker['id']}", headers=admin,
                        json={"role": "wizard"}).status_code == 400
    boss = next(u for u in auth.list_users() if u["username"] == "boss")
    for u in auth.list_users():  # leave "boss" as the only active admin
        if u["role"] == "admin" and u["id"] != boss["id"]:
            auth.update_user(u["id"], disabled=True)
    assert client.patch(f"/api/auth/users/{boss['id']}", headers=admin,
                        json={"role": "doctor"}).status_code == 409
    # Non-admins cannot manage accounts.
    assert client.patch(f"/api/auth/users/{boss['id']}", headers=fresh,
                        json={"disabled": True}).status_code in (401, 403)


def test_login_has_a_per_address_budget(client, monkeypatch):
    import throttle
    monkeypatch.setattr(throttle, "IP_MAX_ATTEMPTS", 3)
    for i in range(3):
        r = client.post("/api/auth/login", data={"username": f"nobody{i}", "password": "x"})
        assert r.status_code == 401
    # A different username from the same address is now refused too.
    r = client.post("/api/auth/login", data={"username": "nobody-else", "password": "x"})
    assert r.status_code == 429


# ---------------------------------------------------------------- peer tokens
def test_peer_tokens_have_a_bounded_lifetime(monkeypatch):
    from clinicaldb import facilities, principal
    monkeypatch.setenv("PEER_SECRET_T", "peer-secret-for-tests-0123456789")
    facilities.upsert({"oid": "2.25.9001", "name": "Peer", "base_url": "http://peer.invalid",
                       "secret_env": "PEER_SECRET_T", "trust_level": "peer", "active": True})
    monkeypatch.setenv("FACILITY_OID", "2.25.9001")
    good = principal.mint_peer_token(audience_oid="2.25.1",
                                     secret="peer-secret-for-tests-0123456789", purpose="TREAT")
    long = principal.mint_peer_token(audience_oid="2.25.1", secret="peer-secret-for-tests-0123456789",
                                     purpose="TREAT", ttl=10 * 365 * 86400)
    monkeypatch.setenv("FACILITY_OID", "2.25.1")
    assert principal.verify_peer_token(good)["facility"]["oid"] == "2.25.9001"
    with pytest.raises(ValueError, match="lifetime"):
        principal.verify_peer_token(long)


# ---------------------------------------------------------------- FHIR transactions
def test_failed_transaction_restores_updated_and_deleted_rows(client):
    h = {**_login(client, "fhir_admin", role="admin"), "Content-Type": "application/fhir+json"}
    F = "/api/fhir/r4"
    pat = client.post(f"{F}/Patient", headers=h, json={
        "resourceType": "Patient", "name": [{"family": "Rollback", "given": ["Test"]}],
        "birthDate": "1970-01-01", "gender": "male"}).json()
    obs = client.post(f"{F}/Condition", headers=h, json={
        "resourceType": "Condition", "subject": {"reference": f"Patient/{pat['id']}"},
        "code": {"text": "Asthma"}}).json()
    keep = client.post(f"{F}/Condition", headers=h, json={
        "resourceType": "Condition", "subject": {"reference": f"Patient/{pat['id']}"},
        "code": {"text": "Diabetes"}}).json()
    bundle = {"resourceType": "Bundle", "type": "transaction", "entry": [
        {"request": {"method": "DELETE", "url": f"Condition/{obs['id']}"}, "resource": {}},
        {"request": {"method": "PUT", "url": f"Condition/{keep['id']}"},
         "resource": {"resourceType": "Condition", "id": keep["id"],
                      "subject": {"reference": f"Patient/{pat['id']}"}, "code": {"text": "CHANGED"}}},
        {"request": {"method": "POST", "url": "Condition"},
         "resource": {"resourceType": "Condition", "subject": {"reference": "Patient/nope"},
                      "code": {"text": "x"}}}]}
    r = client.post(F, headers=h, json=bundle)
    assert r.status_code >= 400
    assert client.get(f"{F}/Condition/{obs['id']}", headers=h).status_code == 200
    assert client.get(f"{F}/Condition/{keep['id']}", headers=h).json()["code"]["text"] == "Diabetes"


# ---------------------------------------------------------------- HL7 listener
def test_mllp_refuses_open_bind_without_access_control(monkeypatch):
    from interop import mllp
    for k in ("HL7_MLLP_ALLOWED_PEERS", "HL7_MLLP_ALLOW_ANY_PEER", "HL7_MLLP_TLS_CERT", "HL7_MLLP_TLS_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="access control"):
        mllp.MllpServer(bind="0.0.0.0", port=0)
    monkeypatch.setenv("HL7_MLLP_ALLOWED_PEERS", "10.0.0.5")
    mllp.MllpServer(bind="0.0.0.0", port=0)  # allowed with an allowlist
    assert mllp.MllpServer(port=0).bind == "127.0.0.1"  # safe default


def test_mllp_closes_idle_connections(monkeypatch):
    import socket
    from interop import mllp
    monkeypatch.setenv("HL7_MLLP_IDLE_SEC", "0.5")
    srv = mllp.MllpServer(bind="127.0.0.1", port=0).start()
    try:
        s = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        s.settimeout(5)
        t0 = time.time()
        assert s.recv(10) == b""  # server hung up on the silent client
        assert time.time() - t0 < 4
        s.close()
    finally:
        srv.stop()


# ---------------------------------------------------------------- readiness
def test_readiness_reports_database(client):
    r = client.get("/api/ready")
    assert r.status_code == 200 and r.json()["ok"] is True
