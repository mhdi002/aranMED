"""Agent tool-calling loop tests."""
from __future__ import annotations

import pytest

from agent import Agent
from memory import MemoryStore


@pytest.fixture
def agent(fresh_registry):
    return Agent(registry=fresh_registry, memory=MemoryStore())


@pytest.mark.asyncio
async def test_agent_simple_text_no_tools(agent, fake_core):
    fake_core.script = [("answer", "Hello, doctor.")]
    res = await agent.run(session_id="s1", user_text="hi")
    assert res.answer == "Hello, doctor."
    assert res.tool_calls == []


@pytest.mark.asyncio
async def test_agent_calls_transcribe_then_structure(agent, fake_core, wav_bytes):
    """End-to-end: audio attached → core decides to transcribe → then to
    structure → finally answers in plain text."""
    fake_core.script = [
        ("tool", "transcribe_audio", {"attachment_id": "audio:0", "language": "fa"}),
        ("tool", "structure_report", {"template_id": "thyroid"}),
        # structure_report internally invokes the core LLM to produce the report:
        ("answer", "Thyroid Sonography:\nFINDINGS: …\nIMPRESSION: 1. …"),
        # then the agent loops once more for the final answer:
        ("answer", "Done — see structured report above."),
    ]
    res = await agent.run(
        session_id="s2",
        user_text="please draft a thyroid sonography report from this dictation",
        attachments={"audio:0": wav_bytes},
    )
    assert res.answer.startswith("Done")
    assert [c["name"] for c in res.tool_calls] == [
        "transcribe_audio", "structure_report"
    ]
    assert res.state.get("transcript")
    assert res.state.get("report")


@pytest.mark.asyncio
async def test_agent_calls_vision(agent, fake_core, png_bytes):
    fake_core.script = [
        ("tool", "describe_image",
         {"attachment_id": "image:0", "question": "Any abnormality?"}),
        ("answer", "Vision finding noted."),
    ]
    res = await agent.run(
        session_id="s3",
        user_text="what's on this CXR?",
        attachments={"image:0": png_bytes},
    )
    assert res.answer == "Vision finding noted."
    assert res.tool_calls[0]["name"] == "describe_image"


@pytest.mark.asyncio
async def test_agent_session_memory_persists(agent, fake_core):
    fake_core.script = [
        ("answer", "Got it: indication is liver mass."),
        ("answer", "Yes, you mentioned a liver mass earlier."),
    ]
    await agent.run(session_id="s4", user_text="indication: liver mass")
    res = await agent.run(session_id="s4", user_text="what was the indication?")
    # The second call should have received the first turn in the messages.
    transcript = fake_core.received[-1]
    user_contents = [m.content for m in transcript if m.role == "user"]
    assert any("liver mass" in c for c in user_contents)
    assert "liver" in res.answer.lower() or "earlier" in res.answer.lower()


@pytest.mark.asyncio
async def test_agent_handles_unknown_tool(agent, fake_core):
    fake_core.script = [
        ("tool", "nonexistent_tool", {}),
        ("answer", "Recovered after error."),
    ]
    res = await agent.run(session_id="s5", user_text="trigger unknown")
    assert res.answer == "Recovered after error."
    assert res.tool_calls[0]["result"]["error"]
