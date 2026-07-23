"""Registry / memory-governor tests."""
from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from registry import Registry


@pytest.mark.asyncio
async def test_registry_lists_providers(fresh_registry):
    info = fresh_registry.list()
    assert set(info["roles"]) == {"core", "asr", "vision"}
    assert info["defaults"]["core"] == "fake-core"


@pytest.mark.asyncio
async def test_registry_get_text_loads_lazily(fresh_registry):
    p = await fresh_registry.get_text("core")
    assert p.name == "fake-core"
    assert p._loaded is True   # noqa: SLF001


@pytest.mark.asyncio
async def test_registry_lru_evicts_when_over_budget(tmp_path: Path):
    # max_warm_models=1 → second access must evict the first.
    yaml = textwrap.dedent("""
        models:
          - role: core
            name: a
            provider: fake_core
            enabled: true
            default: true
            config: {script: []}
          - role: extra
            name: b
            provider: fake_core
            enabled: true
            default: true
            config: {script: []}
        runtime:
          max_warm_models: 1
    """)
    p = tmp_path / "models.yaml"
    p.write_text(yaml)
    Registry._instance = None
    reg = Registry(p)

    a = await reg.get_text("core")
    assert a._loaded                  # noqa: SLF001
    b = await reg._get("extra", None) # noqa: SLF001
    assert b._loaded                  # noqa: SLF001
    # `a` should have been evicted by the LRU governor.
    assert a._loaded is False         # noqa: SLF001
    Registry._instance = None
