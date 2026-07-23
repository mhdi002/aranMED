"""Tool unit tests using the fake registry."""
from __future__ import annotations

import pytest

import templates as templates_mod
from tools import ToolContext, registry as tool_registry


@pytest.fixture
def ctx(fresh_registry, wav_bytes, png_bytes):
    templates_mod.initialise()
    return ToolContext(
        registry=fresh_registry,
        templates=templates_mod,
        attachments={"audio:0": wav_bytes, "image:0": png_bytes},
    )


@pytest.mark.asyncio
async def test_list_templates(ctx):
    res = await tool_registry.get("list_templates").run(ctx)
    assert res.error is None
    assert res.data and res.data["templates"]


@pytest.mark.asyncio
async def test_get_template(ctx):
    res = await tool_registry.get("get_template").run(ctx, template_id="thyroid")
    assert res.error is None
    assert "thyroid" in res.data["body"].lower() or "sonography" in res.data["body"].lower()
    assert ctx.state["template_id"] == "thyroid"


@pytest.mark.asyncio
async def test_transcribe_audio(ctx):
    res = await tool_registry.get("transcribe_audio").run(ctx, attachment_id="audio:0")
    assert res.error is None
    assert "سگمان" in res.data["transcript"]
    assert ctx.state["transcript"] == res.data["transcript"]


@pytest.mark.asyncio
async def test_describe_image(ctx):
    res = await tool_registry.get("describe_image").run(
        ctx, attachment_id="image:0", question="What do you see?"
    )
    assert res.error is None
    assert "perihilar" in res.data["description"].lower()


@pytest.mark.asyncio
async def test_structure_report_uses_state(ctx, fake_core):
    # Stage state as if the LLM had already called transcribe + get_template.
    ctx.state["transcript"] = "تیروئید نرمال است بدون ضایعه"
    ctx.state["template_id"] = "thyroid"
    ctx.state["template_body"] = templates_mod.get_template("thyroid")

    fake_core.script = [("answer",
        "Thyroid Sonography:\nBoth lobes normal.\n"
        "Impression: Normal thyroid.")]
    res = await tool_registry.get("structure_report").run(ctx)
    assert res.error is None
    assert "thyroid" in res.data["report"].lower()
    assert ctx.state["report"]


@pytest.mark.asyncio
async def test_missing_attachment(ctx):
    res = await tool_registry.get("transcribe_audio").run(
        ctx, attachment_id="audio:99")
    assert res.error and "no attachment" in res.error
