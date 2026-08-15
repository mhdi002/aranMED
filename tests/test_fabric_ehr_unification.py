"""Clinical Data Fabric unification tests (docs/core/CLINICAL_DATA_FABRIC_v1.md).

Proves the agent tool-calling path (backend/tools/ehr.py) and the direct
REST path (POST /api/ehr/build in backend/routes.py) now read/write the
same canonical SQLite store, and that the typed backend/fabric/ models
round-trip the existing JSON blob shape without any data-shape migration.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))


@pytest.mark.asyncio
async def test_agent_tool_and_rest_path_share_one_store(fresh_registry, fake_core):
    """build_ehr (agent tool path) writes a record that GET /api/ehr/{id}
    (REST path) can read back, once both are pointed at the same owner."""
    import store
    import templates as templates_mod
    from tools import ToolContext
    from tools.ehr import BuildEHRTool, _system_owner_user_id

    templates_mod.initialise()
    ctx = ToolContext(registry=fresh_registry, templates=templates_mod)

    fake_core.script = [
        (
            "answer",
            '{"patient": {"name": "John Doe", "age": 40, "sex": "M"}, '
            '"encounter": {}, "problems": [], "allergies": [], '
            '"medications": [{"name": "metformin"}], "vitals": {}, "notes": null}',
        )
    ]
    tool = BuildEHRTool()
    res = await tool.run(ctx, patient_info="John Doe, 40yo male, on metformin.")
    assert res.error is None, res.error
    pid = res.data["patient_id"]

    # The REST path's store.get_patient() must see the same record under
    # the system-owner account the tool resolved.
    owner_id = _system_owner_user_id()
    rec = store.get_patient(pid, owner_user_id=owner_id)
    assert rec is not None
    assert rec["id"] == pid


@pytest.mark.asyncio
async def test_get_ehr_tool_reads_rest_built_record(fresh_registry):
    """A record built via the REST path (store.upsert_patient, as
    routes.py::ehr_build does) is readable via the agent's get_ehr tool
    once attributed to the system-owner account."""
    import store
    from tools import ToolContext
    from tools.ehr import GetEHRTool, _system_owner_user_id

    owner_id = _system_owner_user_id()
    store.upsert_patient(
        owner_user_id=owner_id,
        data={"patient": {"name": "Jane Roe"}, "medications": []},
        patient_id="jane-roe",
        language="en",
    )

    ctx = ToolContext(registry=fresh_registry)
    res = await GetEHRTool().run(ctx, patient_id="jane-roe")
    assert res.error is None
    assert res.data["record"]["patient"]["name"] == "Jane Roe"


def test_fabric_models_round_trip_existing_json_shape():
    from fabric import EhrRecord

    raw = {
        "id": "p1",
        "language": "en",
        "created_at": 1.0,
        "updated_at": 2.0,
        "patient": {"name": "A", "age": 30},
        "encounter": {"chief_complaint": "cough"},
        "problems": [{"name": "asthma", "status": "active"}],
        "allergies": [{"substance": "penicillin"}],
        "medications": [{"name": "metformin", "dose": "500mg"}],
        "vitals": {"hr": 80},
        "notes": "stable",
    }
    rec = EhrRecord.from_dict(raw)
    assert rec.patient.name == "A"
    assert rec.problems[0].status == "active"
    assert rec.labs == []
    assert rec.imaging == []
    back = rec.to_dict()
    assert back["patient"]["name"] == "A"
    assert back["labs"] == []
