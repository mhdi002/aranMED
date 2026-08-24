"""Tests for the ASR translation guard.

These cover the mechanism that makes the translation stage trustworthy: numeral
extraction across scripts, fragment splitting, and — the part that matters —
that a fabricating model is REJECTED rather than passed through.

The failure being guarded against is real and observed: see
docs/core/ASR_TRANSLATION_CONFABULATION.md. A model handed the whole transcript
invented anatomy, invented negative findings, invented a measurement, and
inverted a negation, three different ways from identical input.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import english_transcript as et  # noqa: E402

# The real stage-1 Whisper output for the 41.9s sample, verbatim.
PERSIAN = (
    "سلام لطفاً برای بیمار احمد حوشیاری عبدو پلوی که از نظر بررسی فریفلوید "
    "تایبه فرماید که ملد انترولوپ فریفلوید است در عبدو پلوی کویتی و خط بعدیش "
    "هم اویدنس آف تو هایپوکوک استرکتر و اینترنال رویتیکولیشن و "
    "نه با سکولاریتی 50 در 50 در 50 و سی در 51 در اینکه هماتومه را قرار می کنید "
    "مطرح کردیم."
)


class FakeCore:
    """A core LLM stand-in that returns whatever the test tells it to."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, messages, **kw):
        self.calls += 1
        idx = min(self.calls - 1, len(self.responses) - 1)
        text = self.responses[idx]
        if callable(text):
            text = text(messages[-1].content)

        class M:
            content = text
        return M()


class FakeRegistry:
    def __init__(self, core):
        self._core = core

    async def get_text(self, role, name=None):
        return self._core


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# numerals()
# --------------------------------------------------------------------------
def test_numerals_western():
    assert et.numerals("50 x 50 x 50 and 30 x 51") == ["50", "50", "50", "30", "51"]


def test_numerals_persian_indic_digits_normalise():
    # A measurement dictated in Persian digits must compare equal to Western.
    assert et.numerals("۵۰ در ۵۰") == ["50", "50"]
    assert et.numerals("٣٠") == ["30"]


def test_numerals_none():
    assert et.numerals("no measurements here") == []
    assert et.numerals("") == []


def test_numerals_finds_the_real_sample_measurements():
    assert et.numerals(PERSIAN) == ["50", "50", "50", "51"]


# --------------------------------------------------------------------------
# segment()
# --------------------------------------------------------------------------
def test_segment_splits_on_persian_and_english_boundaries():
    parts = et.segment("aaa. bbb، ccc؛ ddd", max_chars=180)
    assert parts == ["aaa.", "bbb،", "ccc؛", "ddd"]


def test_segment_chops_unpunctuated_text_by_word():
    # ASR output often has no punctuation at all; it must still be bounded,
    # because fragment size is what limits the model's room to invent.
    text = " ".join(["word"] * 100)
    parts = et.segment(text, max_chars=50)
    assert len(parts) > 1
    assert all(len(p) <= 50 for p in parts)
    # No words lost in the chopping.
    assert " ".join(parts).split() == text.split()


def test_segment_never_returns_empty_for_nonempty_input():
    assert et.segment("x", max_chars=180) == ["x"]
    assert et.segment("", max_chars=180) == []


# --------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------
def test_english_passthrough_is_not_translated():
    core = FakeCore(["SHOULD NOT BE CALLED"])
    out, rep = run(et.translate_verified("Liver is normal.", registry=FakeRegistry(core)))
    assert out == "Liver is normal."
    assert core.calls == 0
    assert rep["degraded"] is False


def test_faithful_translation_is_accepted():
    # Echo every numeral present in the fragment it is given.
    def echo(user_msg):
        nums = et.numerals(user_msg)
        return "finding " + " x ".join(nums) if nums else "finding"

    core = FakeCore([echo])
    out, rep = run(et.translate_verified(PERSIAN, registry=FakeRegistry(core)))
    assert rep["ok"] is True
    assert rep["degraded"] is False
    assert rep["missing"] == []
    assert rep["invented"] == []
    assert et.numerals(out) == ["50", "50", "50", "51"]


def test_dropped_measurement_is_rejected_and_degrades_to_source():
    # The model returns fluent English that silently omits the numbers.
    core = FakeCore(["The liver appears unremarkable."])
    out, rep = run(et.translate_verified(PERSIAN, registry=FakeRegistry(core)))
    assert rep["ok"] is False
    assert rep["degraded"] is True
    assert "50" in "".join(rep["missing"])
    # Crucially: the caller gets the TRUE source, not the readable falsehood.
    assert out == PERSIAN


def test_invented_measurement_is_rejected():
    # Reproduces the observed run that produced a "53" appearing nowhere in the
    # source — the most dangerous single failure, since 53 mm looks measured.
    core = FakeCore(["measurements recorded at 50, 50, 50, 51, and 53 mm"])
    out, rep = run(et.translate_verified(PERSIAN, registry=FakeRegistry(core)))
    assert rep["ok"] is False
    assert "53" in rep["invented"]
    assert out == PERSIAN


def test_retry_happens_before_giving_up():
    # First pass invents; second pass is faithful. The good one must win.
    def faithful(user_msg):
        nums = et.numerals(user_msg)
        return " x ".join(nums) if nums else "text"

    core = FakeCore(["invented 999 mm", faithful])
    out, rep = run(et.translate_verified(PERSIAN, registry=FakeRegistry(core)))
    assert rep["attempts"] >= 2
    assert rep["degraded"] is False
    assert "999" not in out


def test_verification_can_be_disabled_by_env(monkeypatch=None):
    import os
    os.environ["ASR_TRANSLATE_VERIFY"] = "0"
    try:
        core = FakeCore(["totally unfaithful text with 999"])
        out, rep = run(et.translate_verified(PERSIAN, registry=FakeRegistry(core)))
        # Opt-out is honoured, but the report still tells the truth.
        assert rep["degraded"] is False
        assert "999" in out
    finally:
        os.environ.pop("ASR_TRANSLATE_VERIFY", None)


def test_fragment_failure_keeps_source_for_that_fragment():
    class Boom(FakeCore):
        async def chat(self, messages, **kw):
            raise RuntimeError("provider down")

    core = Boom([])
    out, rep = run(et.translate_verified(PERSIAN, registry=FakeRegistry(core)))
    # Every fragment fell back to its source, so numerals survive and the
    # result is the source text rather than an exception.
    assert "50" in out
    assert rep["missing"] == []


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"ERROR {name}: {type(e).__name__}: {e}")
            else:
                passed += 1
                print(f"pass {name}")
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
