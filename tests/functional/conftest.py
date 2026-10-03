"""Fixtures for the functional (multi-step, real-protocol) suites.

These drive the real FastAPI app through its HTTP surface, real DICOM
associations and real MLLP sockets. Nothing is mocked except the LLM /
vision providers (the shared FakeCoreLLM / FakeVision from tests/conftest).
"""
from __future__ import annotations

import socket
from typing import Iterator

import pytest


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def clinical_env(monkeypatch, tmp_path):
    """Isolated facility identity + storage for one in-process hospital."""
    monkeypatch.setenv("FACILITY_OID", "2.25.1001")
    monkeypatch.setenv("FACILITY_NAME", "Test General Hospital")
    monkeypatch.setenv("PACS_AE_TITLE", "TESTPACS")
    monkeypatch.setenv("PACS_STORAGE_DIR", str(tmp_path / "pacs"))
    monkeypatch.setenv("PACS_DIMSE_ENABLED", "false")
    monkeypatch.setenv("HL7_MLLP_ENABLED", "false")
    return tmp_path


@pytest.fixture
def client(clinical_env, fresh_registry) -> Iterator:
    from fastapi.testclient import TestClient
    import app as app_mod
    from agent import Agent
    # app binds the registry/agent at import; point them at this test's
    # registry (fake core/vision providers) so agent paths are deterministic.
    app_mod.registry = fresh_registry
    app_mod.agent = Agent(registry=fresh_registry, memory=app_mod.memory)
    with TestClient(app_mod.app) as c:
        yield c


def _headers_for(username: str, role: str) -> dict:
    import auth
    try:
        user = auth.create_user(username=username, password="functional-pass-2026",
                                role=role)
    except ValueError:
        user = next(u for u in [auth.authenticate(username=username,
                                                  password="functional-pass-2026")] if u)
    token = auth.create_token({"sub": str(user["id"]), "role": user["role"],
                               "username": user["username"]})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def users(client) -> dict:
    """Bearer headers per role: admin, doctor, radiologist, resident, student."""
    return {role: _headers_for(f"{role}_ft", role)
            for role in ("admin", "doctor", "radiologist", "resident", "student")}
