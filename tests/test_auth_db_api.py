"""End-to-end tests for auth, EHR, alerts and education endpoints.

These exercise the new DB-backed HTTP surface using FastAPI's TestClient,
the fake core/asr/vision providers, and the fresh_db fixture.
"""
from __future__ import annotations

import time


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def _login(http_client, username, password):
    r = http_client.post("/api/auth/login",
                         data={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_register_and_login(http_client):
    r = http_client.post("/api/auth/register",
                         json={"username": "alice", "password": "secret123"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["username"] == "alice"
    assert body["user"]["role"] == "student"   # default self-register role

    # Duplicate registration is rejected.
    r = http_client.post("/api/auth/register",
                         json={"username": "alice", "password": "secret123"})
    assert r.status_code == 400

    # Login with OAuth2 password form.
    r = http_client.post("/api/auth/login",
                         data={"username": "alice", "password": "secret123"})
    assert r.status_code == 200
    token = r.json()["access_token"]

    # /me requires a valid token.
    r = http_client.get("/api/auth/me",
                        headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["username"] == "alice"


def test_self_register_cannot_claim_privileged_roles(http_client):
    import audit
    for role in ("admin", "doctor", "radiologist", "resident"):
        r = http_client.post("/api/auth/register",
                             json={"username": f"mallory_{role}",
                                   "password": "secret123", "role": role})
        assert r.status_code == 403, (role, r.text)
        assert "administrator" in r.json()["detail"]
    # Nothing was created, and every attempt left an audit trail.
    r = http_client.post("/api/auth/login",
                         data={"username": "mallory_admin", "password": "secret123"})
    assert r.status_code == 401
    denials = audit.query(action_prefix="auth.register", outcome="deny", limit=50)
    assert len(denials) >= 4
    assert http_client.get("/api/auth/register-policy").json() == {
        "self_register_roles": ["student"]}


def test_admin_creates_privileged_accounts(http_client):
    import auth
    auth.create_user(username="root_admin", password="adminpass1", role="admin")
    admin = _login(http_client, "root_admin", "adminpass1")
    r = http_client.post("/api/auth/register", headers=admin,
                         json={"username": "dr_new", "password": "doctorpass1",
                               "role": "doctor"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "doctor"
    doc = _login(http_client, "dr_new", "doctorpass1")
    assert http_client.get("/api/ehr", headers=doc).status_code == 200

    # A non-admin token does not unlock privileged roles.
    r = http_client.post("/api/auth/register", headers=doc,
                         json={"username": "dr_evil", "password": "doctorpass1",
                               "role": "admin"})
    assert r.status_code == 403
    # User listing is admin-only.
    assert http_client.get("/api/auth/users", headers=doc).status_code == 403
    names = {u["username"] for u in http_client.get("/api/auth/users", headers=admin).json()["users"]}
    assert {"root_admin", "dr_new"} <= names and "dr_evil" not in names


def test_self_register_roles_configurable(http_client, monkeypatch):
    monkeypatch.setenv("SELF_REGISTER_ROLES", "student,resident")
    r = http_client.post("/api/auth/register",
                         json={"username": "res_one", "password": "secret123",
                               "role": "resident"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "resident"
    monkeypatch.setenv("SELF_REGISTER_ROLES", "")
    r = http_client.post("/api/auth/register",
                         json={"username": "nobody", "password": "secret123"})
    assert r.status_code == 403 and "disabled" in r.json()["detail"]


def test_login_rejects_bad_password(http_client):
    http_client.post("/api/auth/register",
                     json={"username": "bob", "password": "rightpw"})
    r = http_client.post("/api/auth/login",
                         data={"username": "bob", "password": "wrongpw"})
    assert r.status_code == 401


def test_protected_endpoint_without_token(http_client):
    r = http_client.get("/api/ehr")
    assert r.status_code == 401


def test_short_password_rejected(http_client):
    r = http_client.post("/api/auth/register",
                         json={"username": "tiny", "password": "abc"})
    assert r.status_code in (400, 422)


# ---------------------------------------------------------------------------
# EHR
# ---------------------------------------------------------------------------
EHR_JSON = (
    '{"patient":{"name":"Ali Reza","age":62,"sex":"M","mrn":"X1","weight_kg":78},'
    '"encounter":{"date":"2024-01-01","chief_complaint":"cough","summary":null},'
    '"problems":[{"name":"COPD","status":"active"}],'
    '"allergies":[],'
    '"medications":[{"name":"aspirin","dose":"81mg","route":"PO",'
    '"frequency":"daily","frequency_hours":24,"indication":"prophylaxis",'
    '"notes":null}],'
    '"vitals":{"bp":"130/80","hr":78,"temp_c":37.0,"spo2":96,"rr":16},'
    '"notes":null}'
)


def test_ehr_build_get_list_delete(http_client, auth_headers, fake_core):
    fake_core.script = [("answer", EHR_JSON)]
    r = http_client.post("/api/ehr/build",
                         json={"patient_info": "62yo male with cough",
                               "language": "en"},
                         headers=auth_headers)
    assert r.status_code == 200, r.text
    pid = r.json()["patient_id"]
    assert pid == "ali-reza"

    r = http_client.get(f"/api/ehr/{pid}", headers=auth_headers)
    assert r.status_code == 200
    rec = r.json()["record"]
    assert rec["patient"]["name"] == "Ali Reza"
    assert len(rec["medications"]) == 1

    r = http_client.get("/api/ehr", headers=auth_headers)
    assert r.status_code == 200
    items = r.json()["records"]
    assert any(it["id"] == pid for it in items)

    r = http_client.delete(f"/api/ehr/{pid}", headers=auth_headers)
    assert r.status_code == 200
    r = http_client.get(f"/api/ehr/{pid}", headers=auth_headers)
    assert r.status_code == 404


def test_ehr_build_rejects_garbage_llm_output(http_client, auth_headers, fake_core):
    fake_core.script = [("answer", "not json at all")]
    r = http_client.post("/api/ehr/build",
                         json={"patient_info": "x", "language": "en"},
                         headers=auth_headers)
    assert r.status_code == 422


def test_ehr_scoped_per_user(http_client, fake_core):
    # User A creates a record; User B must not see it.
    fake_core.script = [("answer", EHR_JSON), ("answer", EHR_JSON)]
    import auth
    auth.create_user(username="userone", password="pwpwpw", role="doctor")
    auth.create_user(username="usertwo", password="pwpwpw", role="doctor")
    ha = _login(http_client, "userone", "pwpwpw")
    hb = _login(http_client, "usertwo", "pwpwpw")
    http_client.post("/api/ehr/build",
                     json={"patient_info": "x", "language": "en"}, headers=ha)
    assert http_client.get("/api/ehr", headers=ha).json()["records"]
    assert http_client.get("/api/ehr", headers=hb).json()["records"] == []


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
def _seed_patient_with_overdue_med(http_client, auth_headers, fake_core):
    # Make the stored record's medication clearly overdue.
    overdue_json = EHR_JSON.replace(
        '"frequency_hours":24,"indication":"prophylaxis","notes":null}',
        f'"frequency_hours":1,"indication":"prophylaxis","notes":null,'
        f'"last_dose_at":{time.time() - 7200}}}',
    ).replace("aspirin", "Aspirin")
    fake_core.script = [("answer", overdue_json)]
    r = http_client.post("/api/ehr/build",
                         json={"patient_info": "x", "language": "en"},
                         headers=auth_headers)
    return r.json()["patient_id"]


def test_alerts_check_and_send_dry_run(http_client, auth_headers, fake_core):
    pid = _seed_patient_with_overdue_med(http_client, auth_headers, fake_core)

    r = http_client.post("/api/alerts/check",
                         json={"patient_id": pid, "language": "en"},
                         headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["due_count"] >= 1
    assert "Aspirin" in body["summary"] or "aspirin" in body["summary"]

    r = http_client.post("/api/alerts/send",
                         json={"patient_id": pid, "channel": "email",
                               "to": "doc@example.com", "language": "en"},
                         headers=auth_headers)
    assert r.status_code == 200
    sent = r.json()
    assert sent["sent"] is True
    assert sent["delivery"]["dry_run"] is True

    r = http_client.get("/api/alerts", headers=auth_headers)
    assert r.status_code == 200
    assert len(r.json()["alerts"]) >= 1


def test_record_dose_resets_due_state(http_client, auth_headers, fake_core):
    pid = _seed_patient_with_overdue_med(http_client, auth_headers, fake_core)

    r = http_client.post(f"/api/ehr/{pid}/dose",
                         json={"medication": "Aspirin"},
                         headers=auth_headers)
    assert r.status_code == 200

    r = http_client.post("/api/alerts/check",
                         json={"patient_id": pid}, headers=auth_headers)
    assert r.json()["due_count"] == 0


# ---------------------------------------------------------------------------
# Education
# ---------------------------------------------------------------------------
MCQ_JSON = (
    '{"questions":['
    '{"stem":"Most common cause of community-acquired pneumonia?",'
    '"choices":["S. pneumoniae","E. coli","M. tuberculosis","S. aureus"],'
    '"answer_index":0,"explanation":"Strep pneumo is #1."}'
    ']}'
)


def test_education_mcq_creates_quiz(http_client, auth_headers, fake_core):
    fake_core.script = [("answer", MCQ_JSON)]
    r = http_client.post("/api/education/mcq",
                         json={"topic": "pneumonia", "language": "en",
                               "count": 1},
                         headers=auth_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["data"]["questions"][0]["answer_index"] == 0

    r = http_client.get("/api/education/saved", headers=auth_headers)
    items = r.json()["items"]
    assert items and items[0]["kind"] == "mcq"

    qid = items[0]["id"]
    r = http_client.get(f"/api/education/saved/{qid}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["data"]["questions"][0]["choices"][0] == "S. pneumoniae"


def test_education_explain(http_client, auth_headers, fake_core):
    fake_core.script = [("answer", "Pneumonia is an infection of lung parenchyma.")]
    r = http_client.post("/api/education/explain",
                         json={"concept": "pneumonia", "language": "en"},
                         headers=auth_headers)
    assert r.status_code == 200
    assert "Pneumonia" in r.json()["content"]
