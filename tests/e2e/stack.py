"""Launch a complete, seeded AranMed stack for browser E2E tests.

Started by Playwright's ``webServer`` (frontend/playwright.config.js):

  1. fake OpenAI-compatible LLM (deterministic core + vision answers)
  2. backend (uvicorn) on a throw-away database/PACS store, DICOM SCP on
  3. seed: users, synthetic CT/MR studies via STOW-RS, a worklist entry,
     a DICOM node pointing at our own SCP; writes frontend/e2e/.state.json
  4. frontend (``npm start``) in the foreground — Playwright waits for it

Ports come from E2E_* env vars (defaults below); nothing is hard-wired to a
host other than loopback.
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

FRONT_PORT = int(os.environ.get("E2E_FRONTEND_PORT", "3100"))
BACK_PORT = int(os.environ.get("E2E_BACKEND_PORT", "8110"))
LLM_PORT = int(os.environ.get("E2E_LLM_PORT", "8120"))
DIMSE_PORT = int(os.environ.get("E2E_DIMSE_PORT", "11190"))
PEER_PORT = int(os.environ.get("E2E_PEER_PORT", "8111"))
PEER_DIMSE_PORT = int(os.environ.get("E2E_PEER_DIMSE_PORT", "11191"))
PEER_BASE = f"http://127.0.0.1:{PEER_PORT}"
PEER_SECRET = "e2e-peer-shared-secret-0123456789"
ADMIN_PW = "E2E-Admin-Password-2026"
USER_PW = "e2e-user-pass-2026"
BASE = f"http://127.0.0.1:{BACK_PORT}"

procs: list[subprocess.Popen] = []


def _cleanup(*_):
    for p in reversed(procs):
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


atexit.register(_cleanup)
signal.signal(signal.SIGTERM, lambda *_: (_cleanup(), sys.exit(0)))


def wait(url: str, timeout: float = 90) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"timed out waiting for {url}")


def main() -> None:
    work = Path(os.environ.get("E2E_WORKDIR") or tempfile.mkdtemp(prefix="aranmed-e2e-"))
    work.mkdir(parents=True, exist_ok=True)
    py = sys.executable

    procs.append(subprocess.Popen([py, str(ROOT / "tests/e2e/fake_llm.py"), str(LLM_PORT)]))
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL",)}
    env.update({
        "PYTHONPATH": str(ROOT / "backend"),
        "DB_PATH": str(work / "app.db"), "PACS_STORAGE_DIR": str(work / "pacs"),
        "CLINICAL_DATA_DIR": str(work),
        "ASR_AGENT_MODELS_YAML": str(ROOT / "tests/e2e/models.e2e.yaml"),
        "E2E_LLM_URL": f"http://127.0.0.1:{LLM_PORT}/v1",
        "ASR_AGENT_SECRET": "e2e-secret-0123456789abcdef0123456789abcdef",
        "ASR_AGENT_ADMIN_USER": "admin", "ASR_AGENT_ADMIN_PASSWORD": ADMIN_PW,
        "PASSWORD_MIN_LENGTH": "8",
        "FACILITY_OID": "2.25.900", "FACILITY_NAME": "E2E General Hospital",
        "PUBLIC_BASE_URL": BASE,
        "PACS_AE_TITLE": "E2EPACS", "PACS_DIMSE_ENABLED": "true",
        "PACS_DIMSE_BIND": "127.0.0.1", "PACS_DIMSE_PORT": str(DIMSE_PORT),
        "HL7_MLLP_ENABLED": "false",
        "MEDRAG_API_URL": "http://127.0.0.1:9",
    })
    env["PEER_SECRET"] = PEER_SECRET
    procs.append(subprocess.Popen(
        [py, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(BACK_PORT),
         "--log-level", "warning"], cwd=str(ROOT / "backend"), env=env))
    # A second, independent hospital on the network (its own DB / PACS).
    peer_dir = work / "peer"
    peer_dir.mkdir(exist_ok=True)
    penv = {**env, "DB_PATH": str(peer_dir / "app.db"), "PACS_STORAGE_DIR": str(peer_dir / "pacs"),
            "CLINICAL_DATA_DIR": str(peer_dir), "FACILITY_OID": "2.25.901",
            "FACILITY_NAME": "E2E Peer Hospital", "PUBLIC_BASE_URL": PEER_BASE,
            "PACS_AE_TITLE": "E2EPEER", "PACS_DIMSE_PORT": str(PEER_DIMSE_PORT),
            "ASR_AGENT_SECRET": "e2e-peer-secret-0123456789abcdef0123456789abcdef"}
    procs.append(subprocess.Popen(
        [py, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(PEER_PORT),
         "--log-level", "warning"], cwd=str(ROOT / "backend"), env=penv))
    wait(f"{BASE}/api/interop/capabilities")
    wait(f"{PEER_BASE}/api/interop/capabilities")
    seed(work)
    seed_clinical(work)

    fenv = {**os.environ, "BACKEND_URL": BASE, "PORT": str(FRONT_PORT), "HOST": "127.0.0.1"}
    front = subprocess.Popen(["npm", "start"], cwd=str(ROOT / "frontend"), env=fenv)
    procs.append(front)
    front.wait()


def seed(work: Path) -> None:
    from tests.functional.dicom_factory import make_instance, make_study, to_bytes
    from pacs import multipart

    tok = httpx.post(f"{BASE}/api/auth/login",
                     data={"username": "admin", "password": ADMIN_PW}).json()["access_token"]
    H = {"Authorization": f"Bearer {tok}"}
    for user, role in (("e2e_doctor", "doctor"), ("e2e_rad", "radiologist")):
        httpx.post(f"{BASE}/api/auth/register", headers=H,
                   json={"username": user, "password": USER_PW, "role": role}).raise_for_status()

    def stow(dsets):
        bnd = multipart.boundary()
        body = multipart.encode(((to_bytes(d), "application/dicom") for d in dsets), bnd)
        r = httpx.post(f"{BASE}/api/dicom-web/studies", content=body, timeout=60,
                       headers={**H, "Content-Type": multipart.content_type(bnd, "application/dicom")})
        r.raise_for_status()

    ct_uid, ct = make_study(n_series=2, per_series=12, patient_name="Karimi^Ali",
                            patient_id="E2E-CT-1", accession="E2E-ACC-1", study_date="20260921",
                            study_desc="CT CHEST", body_part="CHEST", rows=128, cols=128)
    stow(ct)
    mr_uid, mr = make_study(n_series=1, per_series=8, modality="MR", patient_name="Rahimi^Neda",
                            patient_id="E2E-MR-1", accession="E2E-ACC-2", study_date="20260918",
                            sex="F", study_desc="MRI BRAIN", body_part="HEAD", rows=96, cols=96)
    stow(mr)
    up_uid, up = make_study(n_series=1, per_series=1, patient_name="Upload^Test",
                            patient_id="E2E-UP-1", accession="E2E-ACC-3", modality="CR",
                            study_desc="CR CHEST PA")
    upload_file = work / "upload.dcm"
    upload_file.write_bytes(to_bytes(up[0]))
    httpx.post(f"{BASE}/api/pacs/nodes", headers=H, json={
        "name": "Self (loopback SCP)", "kind": "dimse", "ae_title": "E2EPACS",
        "host": "127.0.0.1", "port": DIMSE_PORT})
    httpx.post(f"{BASE}/api/pacs/worklist", headers=H, json={
        "modality": "CT", "patient_name": "Karimi^Ali", "patient_id": "E2E-CT-1",
        "procedure_description": "CT ABDOMEN", "scheduled_start": "20261010T090000",
        "priority": "STAT"})
    state = {"ct_uid": ct_uid, "mr_uid": mr_uid, "upload_file": str(upload_file),
             "upload_uid": up_uid, "admin_password": ADMIN_PW, "user_password": USER_PW,
             "backend": BASE}
    (ROOT / "frontend/e2e/.state.json").write_text(json.dumps(state, indent=2))


def _facility(base: str, oid: str, name: str, ae: str, dimse_port: int) -> dict:
    return {"oid": oid, "name": name, "kind": "hospital", "base_url": base,
            "fhir_base": base + "/api/fhir/r4", "dicomweb_base": base + "/api/dicom-web",
            "ae_title": ae, "dicom_host": "127.0.0.1", "dicom_port": dimse_port,
            "secret_env": "PEER_SECRET", "trust_level": "peer"}


def seed_clinical(work: Path) -> None:
    """Patient history at the peer, a local chart, EMS inbound, a pending transfer."""
    from tests.functional.dicom_factory import make_study, to_bytes
    from pacs import multipart

    def login(base):
        return {"Authorization": "Bearer " + httpx.post(
            f"{base}/api/auth/login", data={"username": "admin", "password": ADMIN_PW}).json()["access_token"]}
    H, P = login(BASE), login(PEER_BASE)
    httpx.post(f"{BASE}/api/facilities", headers=H,
               json=_facility(PEER_BASE, "2.25.901", "E2E Peer Hospital", "E2EPEER", PEER_DIMSE_PORT))
    httpx.post(f"{PEER_BASE}/api/facilities", headers=P,
               json=_facility(BASE, "2.25.900", "E2E General Hospital", "E2EPACS", DIMSE_PORT))
    httpx.post(f"{PEER_BASE}/api/auth/register", headers=P,
               json={"username": "peer_doc", "password": USER_PW, "role": "doctor"}).raise_for_status()
    PD = {"Authorization": "Bearer " + httpx.post(f"{PEER_BASE}/api/auth/login",
                                                  data={"username": "peer_doc", "password": USER_PW}).json()["access_token"]}

    # History at the peer hospital.
    reg = httpx.post(f"{PEER_BASE}/api/clinical/patients", headers=PD, json={
        "demographics": {"family": "Farahani", "given": "Reza", "birth_date": "1958-04-11", "sex": "male"},
        "mrn": "P-7788", "national_id": "0076543210"}).json()
    ppid = reg["person_id"]
    for kind, body in (("allergies", {"display": "Iodinated contrast", "reaction": "Anaphylaxis", "criticality": "high"}),
                       ("medications", {"display": "Clopidogrel", "dose": "75 mg", "status": "active"}),
                       ("documents", {"title": "Peer discharge summary", "doc_type": "discharge-summary",
                                      "content": "PCI to LAD 2024."})):
        httpx.post(f"{PEER_BASE}/api/clinical/patients/{ppid}/{kind}", headers=PD, json=body)
    uid, ds = make_study(n_series=1, per_series=4, patient_name="Farahani^Reza", patient_id="P-7788",
                         issuer="2.25.901", birth_date="19580411", study_desc="CT CORONARY", rows=96, cols=96)
    bnd = multipart.boundary()
    httpx.post(f"{PEER_BASE}/api/dicom-web/studies", headers={**P, "Content-Type": multipart.content_type(bnd, "application/dicom")},
               content=multipart.encode(((to_bytes(d), "application/dicom") for d in ds), bnd), timeout=60)

    # The same man known locally (national id) with some local data + labs for trends.
    DH = {"Authorization": "Bearer " + httpx.post(f"{BASE}/api/auth/login",
                                                  data={"username": "e2e_doctor", "password": USER_PW}).json()["access_token"]}
    loc = httpx.post(f"{BASE}/api/clinical/patients", headers=DH, json={
        "demographics": {"family": "Farahani", "given": "Reza", "birth_date": "1958-04-11", "sex": "male"},
        "mrn": "L-1001", "national_id": "0076543210"}).json()
    lpid = loc["person_id"]
    for i, (d, v) in enumerate((("2026-06-01", 1.1), ("2026-07-01", 1.4), ("2026-08-01", 1.8))):
        httpx.post(f"{BASE}/api/clinical/patients/{lpid}/observations", headers=DH, json={
            "category": "laboratory", "code_system": "http://loinc.org", "code": "2160-0", "display": "Creatinine",
            "value_num": v, "unit": "mg/dL", "ref_low": 0.6, "ref_high": 1.2, "effective": d})
    httpx.post(f"{BASE}/api/clinical/patients/{lpid}/encounters", headers=DH, json={
        "class": "AMB", "type_text": "Cardiology clinic visit", "reason": "Exertional chest pain",
        "start_at": "2026-08-01T09:30:00", "department": "Cardiology", "attending": "Dr. Rostami",
        "status": "finished"}).raise_for_status()
    httpx.post(f"{BASE}/api/clinical/patients/{lpid}/conditions", headers=DH,
               json={"display": "Coronary artery disease", "clinical_status": "active"})
    httpx.post(f"{BASE}/api/clinical/patients/{lpid}/documents", headers=DH, json={
        "title": "Cardiology note", "doc_type": "progress-note", "content": "Stable angina, on DAPT."})
    # A legacy free-text EHR (dictation pipeline) for the EHR / alerts pages.
    httpx.post(f"{BASE}/api/ehr/build", headers=DH, timeout=60, json={
        "patient_info": "54F, cough and fever 4 days, CAP RLL, on azithromycin and metformin, PCN allergy",
        "language": "en"}).raise_for_status()
    # A restricted record.
    vip = httpx.post(f"{BASE}/api/clinical/patients", headers=DH, json={
        "demographics": {"family": "Vip", "given": "Staff", "birth_date": "1970-01-01"}, "mrn": "VIP-9"}).json()
    httpx.post(f"{BASE}/api/clinical/patients/{vip['person_id']}/consents", headers=DH,
               json={"category": "restricted", "status": "active"})
    # EMS inbound (posted by a local user, NEMSIS JSON).
    httpx.post(f"{BASE}/api/ems/notify", headers=DH, json={
        "eResponse.03": "E2E-INC-1", "eResponse.14": "MEDIC-21", "ePatient.02": "Karimi", "ePatient.03": "Ali",
        "ePatient.13": "9906003", "ePatient.17": "1980-01-01", "eSituation.04": "Chest pain",
        "eSituation.13": "2813001", "eTimes.11": "2026-10-03T22:10:00",
        "vitals": [{"eVitals.06": 84, "eVitals.07": 50, "eVitals.10": 128, "eVitals.12": 89}],
        "eMedications.03": ["Aspirin 300 mg"]})
    # A transfer request from the peer to us (incoming here, awaiting acceptance).
    httpx.post(f"{PEER_BASE}/api/transfers", headers=PD, timeout=60, json={
        "person_id": ppid, "to_facility": "2.25.900", "urgency": "urgent",
        "reason": "Primary PCI", "clinical_summary": "NSTEMI", "include_imaging": True})
    st = json.loads((ROOT / "frontend/e2e/.state.json").read_text())
    st.update({"local_person": lpid, "peer_person": ppid, "vip_person": vip["person_id"],
               "peer_study": uid, "peer_backend": PEER_BASE})
    (ROOT / "frontend/e2e/.state.json").write_text(json.dumps(st, indent=2))


if __name__ == "__main__":
    main()
