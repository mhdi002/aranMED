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
    procs.append(subprocess.Popen(
        [py, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(BACK_PORT),
         "--log-level", "warning"], cwd=str(ROOT / "backend"), env=env))
    wait(f"{BASE}/api/interop/capabilities")
    seed(work)

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
        httpx.post(f"{BASE}/api/auth/register",
                   json={"username": user, "password": USER_PW, "role": role})

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


if __name__ == "__main__":
    main()
