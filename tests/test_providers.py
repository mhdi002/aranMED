"""Provider-level unit tests."""
from __future__ import annotations

import asyncio

import pytest

from providers.base import ChatMessage


@pytest.mark.asyncio
async def test_fake_core_basic_answer(fake_core):
    fake_core.script = [("answer", "hello world")]
    out = await fake_core.chat([ChatMessage(role="user", content="hi")])
    assert out.content == "hello world"
    assert out.tool_calls == []


@pytest.mark.asyncio
async def test_fake_core_emits_tool_call(fake_core):
    fake_core.script = [
        ("tool", "list_templates", {}),
        ("answer", "see above"),
    ]
    first = await fake_core.chat([ChatMessage(role="user", content="?")])
    assert first.tool_calls and first.tool_calls[0].name == "list_templates"
    second = await fake_core.chat([ChatMessage(role="user", content="?")])
    assert second.content == "see above"


@pytest.mark.asyncio
async def test_fake_asr_round_trip(fake_asr, wav_bytes):
    text = await fake_asr.transcribe(wav_bytes, language="fa")
    assert "سگمان" in text


@pytest.mark.asyncio
async def test_fake_vision_requires_image(fake_vision, png_bytes):
    msg_no_img = ChatMessage(role="user", content="what?")
    with pytest.raises(AssertionError):
        await fake_vision.chat([msg_no_img])
    msg_img = ChatMessage(role="user", content="what?", images=[png_bytes])
    out = await fake_vision.chat([msg_img])
    assert "perihilar" in out.content.lower()
