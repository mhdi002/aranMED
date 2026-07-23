"""Thin HTTP client for the MedicalRAG microservice.

MedicalRAG is a separate service (black box). This module only calls its
documented HTTP API:

  GET  {MEDRAG_API_URL}/health
  POST {MEDRAG_API_URL}/ask          JSON {"query", "specialty"?}
  POST {MEDRAG_API_URL}/ask/image    JSON {"query"?, "image_path"}

All connection settings come from environment / config — no deploy paths
or hosts are hardcoded here.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

log = logging.getLogger("integrations.medrag")


class MedragError(RuntimeError):
    """Raised when MedicalRAG is unreachable or returns an error."""


def _base_url() -> str:
    # Prefer MEDRAG_API_URL (Pair A contract); MEDICALRAG_URL is an alias.
    raw = (
        os.getenv("MEDRAG_API_URL")
        or os.getenv("MEDICALRAG_URL")
        or ""
    ).strip().rstrip("/")
    if not raw:
        raise MedragError(
            "MEDRAG_API_URL is not set. Point it at the MedicalRAG service "
            "(see .env.example)."
        )
    return raw


def _timeout() -> float:
    return float(os.getenv("MEDRAG_TIMEOUT_SEC", "120"))


class MedragClient:
    """Async HTTP adapter around MedicalRAG /ask, /ask/image, /health."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        timeout_sec: Optional[float] = None,
    ) -> None:
        self.base_url = (base_url or _base_url()).rstrip("/")
        self.timeout = float(timeout_sec if timeout_sec is not None else _timeout())

    async def health(self) -> dict[str, Any]:
        url = f"{self.base_url}/health"
        # Keep health probes short even if ask() uses a larger timeout.
        probe_timeout = min(float(self.timeout), 3.0)
        try:
            async with httpx.AsyncClient(timeout=probe_timeout) as c:
                r = await c.get(url)
                r.raise_for_status()
                data = r.json()
                return {
                    "ok": True,
                    "url": self.base_url,
                    **(data if isinstance(data, dict) else {"raw": data}),
                }
        except MedragError:
            raise
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "url": self.base_url, "detail": str(e)}

    async def ask(
        self,
        query: str,
        *,
        specialty: Optional[str] = None,
    ) -> dict[str, Any]:
        """POST /ask — returns MedicalRAG JSON (answer, sources, …)."""
        q = (query or "").strip()
        if not q:
            raise MedragError("query is empty")
        body: dict[str, Any] = {"query": q, "specialty": specialty}
        url = f"{self.base_url}/ask"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(url, json=body)
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPStatusError as e:
            raise MedragError(
                f"MedicalRAG /ask HTTP {e.response.status_code}: "
                f"{e.response.text[:300]}"
            ) from e
        except MedragError:
            raise
        except Exception as e:  # noqa: BLE001
            raise MedragError(f"MedicalRAG /ask failed: {e}") from e
        if not isinstance(data, dict):
            raise MedragError("MedicalRAG /ask returned non-object JSON")
        return data

    async def ask_image(
        self,
        image_path: str,
        *,
        query: str = "",
    ) -> dict[str, Any]:
        """POST /ask/image — exam OCR path for png/jpg/jpeg; else image+query."""
        path = (image_path or "").strip()
        if not path:
            raise MedragError("image_path is empty")
        body: dict[str, Any] = {"query": query or "", "image_path": path}
        url = f"{self.base_url}/ask/image"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(url, json=body)
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPStatusError as e:
            raise MedragError(
                f"MedicalRAG /ask/image HTTP {e.response.status_code}: "
                f"{e.response.text[:300]}"
            ) from e
        except MedragError:
            raise
        except Exception as e:  # noqa: BLE001
            raise MedragError(f"MedicalRAG /ask/image failed: {e}") from e
        if not isinstance(data, dict):
            raise MedragError("MedicalRAG /ask/image returned non-object JSON")
        return data

    @staticmethod
    def format_answer(payload: dict[str, Any]) -> str:
        """Flatten MedicalRAG response into readable assistant text."""
        answer = (payload.get("answer") or "").strip()
        sources = payload.get("sources") or []
        lines = [answer] if answer else []
        if sources:
            lines.append("")
            lines.append("Sources:")
            for s in sources[:8]:
                title = s.get("title") or s.get("book") or "?"
                page = s.get("page")
                score = s.get("score")
                bit = f"  [{s.get('n', '?')}] {title}"
                if page is not None:
                    bit += f" (p.{page})"
                if score is not None:
                    bit += f" score={score}"
                lines.append(bit)
        alerts = payload.get("rule_alerts") or []
        if alerts:
            lines.append("")
            lines.append("Rule alerts:")
            for a in alerts[:5]:
                lines.append(f"  - {a.get('message') or a}")
        return "\n".join(lines).strip() or "(empty MedicalRAG answer)"


_client: Optional[MedragClient] = None


def get_medrag_client() -> MedragClient:
    """Lazy singleton; rebuilds if MEDRAG_API_URL changes between calls."""
    global _client
    url = _base_url()
    if _client is None or _client.base_url != url:
        _client = MedragClient(base_url=url)
    return _client
