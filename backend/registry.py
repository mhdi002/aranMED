"""Dynamic model registry.

Reads ``models.yaml``, instantiates the right provider per role and acts as a
tiny memory governor: it tracks LRU usage and unloads cold models when the
warm-set limit (or VRAM budget) is exceeded.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import yaml

from providers import ASRProvider, BaseProvider, TextProvider, VisionProvider
from providers.hf_asr import HFASRProvider
from providers.hf_text import HFTextProvider
from providers.hf_vision import HFVisionProvider
from providers.llamacpp import LlamaCppPythonProvider, LlamaCppServerProvider
from providers.ollama import OllamaProvider
from providers.omniasr import OmniASRProvider
from providers.openai_compat import OpenAIProvider
from providers.crispasr import CrispASRProvider
from providers.codeswitch_asr import CodeSwitchASRProvider
from providers.lid_segment_provider import LIDSegmentASRProvider
from providers.faster_whisper_asr import FasterWhisperASRProvider
from providers.triton_asr import TritonASRProvider

log = logging.getLogger("registry")

PROVIDER_CLS: dict[str, type[BaseProvider]] = {
    "ollama":           OllamaProvider,
    "openai":           OpenAIProvider,
    "llamacpp_server":  LlamaCppServerProvider,
    "llamacpp_python":  LlamaCppPythonProvider,
    "hf_text":          HFTextProvider,
    "hf_asr":           HFASRProvider,
    "hf_vision":        HFVisionProvider,
    "omniasr":          OmniASRProvider,
    "crispasr":         CrispASRProvider,
    "codeswitch_asr":   CodeSwitchASRProvider,
    "lid_segment_asr":  LIDSegmentASRProvider,
    "faster_whisper":   FasterWhisperASRProvider,
    "triton_asr":       TritonASRProvider,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# Supports both ``${VAR}`` and bash-style ``${VAR:-default}`` so YAML configs
# never need to hardcode machine-specific paths — unset vars fall back to a
# sane default (or empty string) instead of leaving a dead literal path.
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-(.*?))?\}")


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _as_bool(value, *, default: bool) -> bool:
    """Interpret a YAML bool or an interpolated string as a boolean.

    ``enabled: ${OLLAMA_ENABLED:-true}`` is a string by the time it gets
    here, and Python would read the string "false" as True. Anything
    unrecognised falls back to *default* rather than guessing.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    log.warning("registry: cannot read %r as a boolean; using %s", value, default)
    return default


def _interp_env(value):
    """Recursively replace ``${ENV_VAR}`` / ``${ENV_VAR:-default}`` tokens
    inside config strings."""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            return os.environ.get(name, default if default is not None else "")
        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interp_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interp_env(v) for v in value]
    return value


def _vram_used_gb() -> float:
    try:
        import torch
        if not torch.cuda.is_available():
            return 0.0
        return torch.cuda.memory_allocated() / 1024 ** 3
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class Registry:
    """Singleton lookup table of role → provider instance."""

    _instance: Optional["Registry"] = None

    def __init__(self, yaml_path: Path) -> None:
        self.yaml_path = yaml_path
        self._providers: dict[str, BaseProvider] = {}        # name -> instance
        self._role_index: dict[str, list[str]] = {}          # role -> names
        self._defaults: dict[str, str] = {}                  # role -> default name
        self._lru: list[str] = []                            # MRU last
        self.runtime: dict = {}
        self._lock = asyncio.Lock()
        self._load()

    @classmethod
    def get(cls, yaml_path: Optional[Path] = None) -> "Registry":
        if cls._instance is None:
            # ASR_AGENT_MODELS_YAML selects an alternative registry file (as
            # documented in models.yaml); default is backend/models.yaml.
            env_path = os.environ.get("ASR_AGENT_MODELS_YAML", "").strip()
            path = yaml_path or (Path(env_path) if env_path else Path(__file__).parent / "models.yaml")
            cls._instance = Registry(path)
        return cls._instance

    # --- yaml ----------------------------------------------------------
    def _load(self) -> None:
        log.info("loading registry from %s", self.yaml_path)
        with open(self.yaml_path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        self.runtime = doc.get("runtime", {}) or {}

        for entry in doc.get("models", []) or []:
            # enabled/default may arrive as a real YAML bool, or as a string
            # once `${VAR:-true}` has been interpolated. A bare truthiness
            # test would read the string "false" as True -- which silently
            # enables a provider the operator explicitly turned off.
            if not _as_bool(_interp_env(entry.get("enabled", True)), default=True):
                continue
            role = entry["role"]
            name = entry["name"]
            provider_kind = entry["provider"]
            cls = PROVIDER_CLS.get(provider_kind)
            if cls is None:
                log.warning("unknown provider %s (skipped)", provider_kind)
                continue
            cfg = _interp_env(entry.get("config", {}) or {})
            inst = cls(name=name, config=cfg)
            inst.role = role  # override class default with the YAML-declared role
            self._providers[name] = inst
            self._role_index.setdefault(role, []).append(name)
            if (_as_bool(_interp_env(entry.get("default", False)), default=False)
                    and role not in self._defaults):
                self._defaults[role] = name

        for role, names in self._role_index.items():
            if role not in self._defaults:
                self._defaults[role] = names[0]
        log.info("registry: %d providers, defaults=%s",
                 len(self._providers), self._defaults)

    # --- queries -------------------------------------------------------
    def list(self) -> dict:
        return {
            "providers": {n: p.info() for n, p in self._providers.items()},
            "roles": self._role_index,
            "defaults": self._defaults,
            "runtime": self.runtime,
            "vram_used_gb": round(_vram_used_gb(), 2),
        }

    def has_role(self, role: str) -> bool:
        return role in self._role_index

    async def get_text(self, role: str = "core", *, name: Optional[str] = None) -> TextProvider:
        p = await self._get(role, name)
        if not isinstance(p, TextProvider):
            raise TypeError(f"provider {p.name} is not a TextProvider")
        return p

    async def get_vision(self, role: str = "vision", *, name: Optional[str] = None) -> VisionProvider:
        p = await self._get(role, name)
        if not isinstance(p, VisionProvider) and not getattr(p, "supports_vision", False):
            raise TypeError(f"provider {p.name} is not vision-capable")
        return p  # type: ignore[return-value]

    async def get_asr(self, role: str = "asr", *, name: Optional[str] = None) -> ASRProvider:
        p = await self._get(role, name)
        if not isinstance(p, ASRProvider):
            raise TypeError(f"provider {p.name} is not an ASRProvider")
        return p

    async def _get(self, role: str, name: Optional[str]) -> BaseProvider:
        target = name or self._defaults.get(role)
        if target is None or target not in self._providers:
            raise KeyError(f"no provider for role={role!r} name={name!r}")
        async with self._lock:
            await self._enforce_budget(target)
            inst = self._providers[target]
            await inst.ensure_loaded()
            self._touch(target)
            return inst

    # --- LRU + budget --------------------------------------------------
    def _touch(self, name: str) -> None:
        if name in self._lru:
            self._lru.remove(name)
        self._lru.append(name)

    async def _enforce_budget(self, incoming: str) -> None:
        max_warm = int(self.runtime.get("max_warm_models", 0) or 0)
        vram_budget = float(self.runtime.get("vram_budget_gb", 0) or 0)
        warm = [n for n in self._lru if self._providers[n]._loaded]  # noqa: SLF001

        # If already warm we don't need to evict for it.
        if incoming in warm:
            return

        def need_evict() -> bool:
            if max_warm and len([n for n in self._lru
                                  if self._providers[n]._loaded]) >= max_warm:  # noqa: SLF001
                return True
            if vram_budget and _vram_used_gb() > vram_budget:
                return True
            return False

        # Evict from coldest to warmest until budget satisfied.
        for cold in list(self._lru):
            if cold == incoming:
                continue
            if not need_evict():
                break
            inst = self._providers[cold]
            if inst._loaded:  # noqa: SLF001
                log.info("registry: evicting %s (LRU)", cold)
                try:
                    await inst.unload()
                except Exception as e:  # noqa: BLE001
                    log.warning("unload %s failed: %s", cold, e)
            self._lru.remove(cold)

    # --- health --------------------------------------------------------
    async def health(self) -> dict:
        """Probe every provider concurrently with a per-provider timeout.

        Probing serially made this endpoint as slow as the sum of all
        providers' timeouts: a single unreachable optional provider (e.g. a
        Triton server that isn't running) added ~3 s to every call. Providers
        are independent, so fan out and bound each one. Override the budget
        with PROVIDER_HEALTH_TIMEOUT_SEC.
        """
        timeout = float(os.getenv("PROVIDER_HEALTH_TIMEOUT_SEC", "2"))

        async def probe(name: str, inst) -> tuple[str, dict]:
            try:
                h = await asyncio.wait_for(inst.health(), timeout=timeout)
                return name, {**inst.info(), "health": h}
            except asyncio.TimeoutError:
                return name, {
                    **inst.info(),
                    "health": {"ok": False, "detail": f"health probe timed out after {timeout}s"},
                }
            except Exception as e:  # noqa: BLE001
                return name, {**inst.info(), "health": {"ok": False, "detail": str(e)}}

        results = await asyncio.gather(
            *(probe(n, i) for n, i in self._providers.items())
        )
        out: dict[str, dict] = dict(results)
        return {
            "vram_used_gb": round(_vram_used_gb(), 2),
            "warm": [n for n in self._lru if self._providers[n]._loaded],  # noqa: SLF001
            "providers": out,
            "defaults": self._defaults,
            "ts": time.time(),
        }
