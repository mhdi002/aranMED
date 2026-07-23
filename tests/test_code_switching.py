"""Test code-switching support in ASR providers."""
from __future__ import annotations

import pytest

from providers.crispasr import CrispASRProvider
from providers.omniasr import OmniASRProvider
from providers.hf_asr import HFASRProvider


def test_crispasr_force_language_disabled():
    """Test that CrispASR doesn't force language when force_language=False."""
    provider = CrispASRProvider(
        name="test-crisp",
        config={
            "model_path": "/fake/model.gguf",
            "force_language": False,
        }
    )
    assert provider.force_language is False


def test_crispasr_force_language_enabled():
    """Test that CrispASR can force language when force_language=True."""
    provider = CrispASRProvider(
        name="test-crisp",
        config={
            "model_path": "/fake/model.gguf",
            "force_language": True,
        }
    )
    assert provider.force_language is True


def test_crispasr_force_language_default():
    """Test that CrispASR defaults to force_language=False."""
    provider = CrispASRProvider(
        name="test-crisp",
        config={
            "model_path": "/fake/model.gguf",
        }
    )
    assert provider.force_language is False


def test_omniasr_force_language_disabled():
    """Test that OmniASR doesn't force language when force_language=False."""
    provider = OmniASRProvider(
        name="test-omni",
        config={
            "model_card": "omniASR_LLM_7B",
            "force_language": False,
        }
    )
    assert provider.force_language is False


def test_omniasr_force_language_enabled():
    """Test that OmniASR can force language when force_language=True."""
    provider = OmniASRProvider(
        name="test-omni",
        config={
            "model_card": "omniASR_LLM_7B",
            "force_language": True,
        }
    )
    assert provider.force_language is True


def test_omniasr_force_language_default():
    """Test that OmniASR defaults to force_language=False."""
    provider = OmniASRProvider(
        name="test-omni",
        config={
            "model_card": "omniASR_LLM_7B",
        }
    )
    assert provider.force_language is False


def test_hf_asr_force_language_disabled():
    """Test that HF ASR doesn't force language when force_language=False."""
    provider = HFASRProvider(
        name="test-hf",
        config={
            "model_id": "openai/whisper-small",
            "force_language": False,
        }
    )
    assert provider.force_language is False


def test_hf_asr_force_language_enabled():
    """Test that HF ASR can force language when force_language=True."""
    provider = HFASRProvider(
        name="test-hf",
        config={
            "model_id": "openai/whisper-small",
            "force_language": True,
        }
    )
    assert provider.force_language is True


def test_hf_asr_force_language_default():
    """Test that HF ASR defaults to force_language=False."""
    provider = HFASRProvider(
        name="test-hf",
        config={
            "model_id": "openai/whisper-small",
        }
    )
    assert provider.force_language is False


def test_faster_whisper_force_language_default():
    """FasterWhisper defaults to multilingual auto-detect."""
    from providers.faster_whisper_asr import FasterWhisperASRProvider

    provider = FasterWhisperASRProvider(
        name="test-fw",
        config={"model_size": "large-v3-turbo"},
    )
    assert provider.force_language is False
    assert provider.vad_filter is True
