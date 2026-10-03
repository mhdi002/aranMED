"""Run complete AranMed hospital instances as real server processes.

Each ``Hospital`` is a separate uvicorn process with its own database,
PACS storage, facility OID, AE title, DICOM port and MLLP port — exactly
how two hospitals would run — so inter-hospital tests exercise real HTTP,
real DICOM and real HL7 between independent deployments.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from tests.functional.conftest import free_port

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
ADMIN_PASSWORD = "Hospital-Admin-Pass-2026"


class Hospital:
    def __init__(self, name: str, oid: str, workdir: Path, *, ae_title: str,
                 extra_env: Optional[dict[str, str]] = None, dimse: bool = True,
                 mllp: bool = False) -> None:
        self.name, self.oid, self.ae_title = name, oid, ae_title
        self.dir = workdir / oid
        self.dir.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.dimse_port = free_port()
        self.mllp_port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("ASR_AGENT_REGISTRY_YAML", "DATABASE_URL")}
        self.env.update({
            "PYTHONPATH": str(BACKEND),
            "DB_PATH": str(self.dir / "app.db"),
            "PACS_STORAGE_DIR": str(self.dir / "pacs"),
            "CLINICAL_DATA_DIR": str(self.dir),
            "FACILITY_OID": oid, "FACILITY_NAME": name,
            "PUBLIC_BASE_URL": self.base,
            "PACS_AE_TITLE": ae_title,
            "PACS_DIMSE_ENABLED": "true" if dimse else "false",
            "PACS_DIMSE_BIND": "127.0.0.1", "PACS_DIMSE_PORT": str(self.dimse_port),
            "PACS_DIMSE_PUBLIC_HOST": "127.0.0.1",
            "HL7_MLLP_ENABLED": "true" if mllp else "false",
            "HL7_MLLP_BIND": "127.0.0.1", "HL7_MLLP_PORT": str(self.mllp_port),
            "HL7_MLLP_PUBLIC_HOST": "127.0.0.1",
            "ASR_AGENT_SECRET": f"secret-{oid}-0123456789abcdef0123456789",
            "ASR_AGENT_ADMIN_USER": "admin", "ASR_AGENT_ADMIN_PASSWORD": ADMIN_PASSWORD,
            "PASSWORD_MIN_LENGTH": "6",
            "OLLAMA_ENABLED": "false",
        })
        if extra_env:
            self.env.update(extra_env)
        self.proc: Optional[subprocess.Popen] = None
        self._tokens: dict[str, str] = {}

    # -- lifecycle -------------------------------------------------------------
    def start(self, timeout: float = 60.0) -> "Hospital":
        log = open(self.dir / "server.log", "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--log-level", "warning"],
            cwd=str(BACKEND), env=self.env, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.name} exited:\n{self.log_tail()}")
            try:
                if httpx.get(f"{self.base}/api/live", timeout=1).status_code == 200:
                    # Startup hooks (facility seed, SCP) finish before readiness.
                    caps = httpx.get(f"{self.base}/api/interop/capabilities", timeout=5)
                    if caps.status_code == 200:
                        return self
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        raise RuntimeError(f"{self.name} did not become ready:\n{self.log_tail()}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def log_tail(self, n: int = 60) -> str:
        p = self.dir / "server.log"
        return "\n".join(p.read_text(errors="replace").splitlines()[-n:]) if p.exists() else ""

    # -- HTTP helpers ------------------------------------------------------------
    def token(self, role: str = "admin") -> str:
        if role in self._tokens:
            return self._tokens[role]
        if role == "admin":
            r = httpx.post(f"{self.base}/api/auth/login",
                           data={"username": "admin", "password": ADMIN_PASSWORD}, timeout=10)
        else:
            user = f"{role}_{self.ae_title.lower()}"
            r = httpx.post(f"{self.base}/api/auth/register",
                           json={"username": user, "password": "role-pass-2026", "role": role},
                           timeout=10)
            if r.status_code == 400:
                r = httpx.post(f"{self.base}/api/auth/login",
                               data={"username": user, "password": "role-pass-2026"}, timeout=10)
        r.raise_for_status()
        self._tokens[role] = r.json()["access_token"]
        return self._tokens[role]

    def h(self, role: str = "admin") -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(role)}"}

    def get(self, path: str, role: str = "admin", **kw) -> httpx.Response:
        return httpx.get(self.base + path, headers={**self.h(role), **kw.pop("headers", {})},
                         timeout=kw.pop("timeout", 60), **kw)

    def post(self, path: str, role: str = "admin", **kw) -> httpx.Response:
        return httpx.post(self.base + path, headers={**self.h(role), **kw.pop("headers", {})},
                          timeout=kw.pop("timeout", 120), **kw)

    def facility_record(self, *, secret_env: str, trust_level: str = "peer",
                        kind: str = "hospital") -> dict[str, Any]:
        """How *another* hospital registers this one as a peer."""
        return {"oid": self.oid, "name": self.name, "kind": kind, "ae_title": self.ae_title,
                "dicom_host": "127.0.0.1", "dicom_port": self.dimse_port,
                "base_url": self.base,
                "fhir_base": self.base + "/api/fhir/r4",
                "dicomweb_base": self.base + "/api/dicom-web",
                "mllp_host": "127.0.0.1", "mllp_port": self.mllp_port,
                "secret_env": secret_env, "trust_level": trust_level}
