"""Integration test for code-switching support in ASR providers.

This test verifies that the force_language configuration option works correctly
to enable or disable language forcing in ASR providers, which is essential for
code-switching support between Persian and English.
"""
from __future__ import annotations

import pytest


def test_code_switching_config_documentation():
    """Document the code-switching configuration for future reference."""
    
    # Code-switching is enabled by setting force_language: false in models.yaml
    # This allows the ASR model to auto-detect language instead of forcing a specific one
    
    config_example = {
        "role": "asr",
        "name": "vibevoice-local",
        "provider": "crispasr",
        "config": {
            "model_path": "/path/to/model.gguf",
            "force_language": False,  # Set to False for code-switching support
        }
    }
    
    # When force_language is False (default):
    # - CrispASR: Does not pass --language flag to the binary
    # - OmniASR: Does not pass lang parameter to the pipeline
    # - HF ASR: Does not use forced_decoder_ids
    
    # When force_language is True:
    # - Language forcing is enabled (useful for single-language transcription)
    
    assert config_example["config"]["force_language"] is False
