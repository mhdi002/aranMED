"""Terminology binding for the FHIR export.

The source text is dictated free-form, so the FHIR projection previously
emitted every condition, allergy and medication as a ``text``-only
``CodeableConcept``. That is honest but not interoperable: a receiving system
cannot reason over free text.

This module adds a **conservative** binding layer:

* Bindings are **data** (``backend/data/terminology.json``, overridable with
  ``TERMINOLOGY_FILE``) — SNOMED CT for conditions and allergies, RxNorm for
  medications, each concept carrying the spellings that map to it, including
  Persian.
* Matching is **exact on a normalised form** (lowercased, punctuation and
  extra whitespace stripped), plus an optional narrow substring pass for
  medications, where a dictated string like "amoxicillin 500 mg" carries the
  ingredient plus a dose. No fuzzy scoring, no stemming, no nearest-neighbour.
* **Unmatched terms stay text-only.** A wrong code is worse than no code — it
  asserts a clinical fact nobody stated. Every emitted concept keeps its
  original text alongside the coding, so the dictated wording is never lost.
* An external terminology server would supersede this file. The shape here
  (normalise → look up → fall back to text) is deliberately the same one such
  a client would slot into.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

import config

log = logging.getLogger("terminology")

_PUNCT = re.compile(r"[^\w\s؀-ۿ]+", re.UNICODE)
_WS = re.compile(r"\s+")

_index: Optional[dict[str, dict[str, Any]]] = None
_lock = threading.Lock()


def terminology_path() -> Path:
    override = os.environ.get("TERMINOLOGY_FILE", "").strip()
    if override:
        return Path(override)
    return config.ROOT / "data" / "terminology.json"


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Persian-safe."""
    if not text:
        return ""
    s = _PUNCT.sub(" ", str(text).lower())
    return _WS.sub(" ", s).strip()


def _load() -> dict[str, dict[str, Any]]:
    global _index
    with _lock:
        if _index is not None:
            return _index
        path = terminology_path()
        built: dict[str, dict[str, Any]] = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("terminology: no bindings loaded from %s (%s) — "
                        "FHIR concepts will stay text-only", path, e)
            _index = {}
            return _index

        for domain, spec in doc.items():
            if domain.startswith("_") or not isinstance(spec, dict):
                continue
            system = spec.get("system")
            lookup: dict[str, dict[str, str]] = {}
            for concept in spec.get("concepts") or []:
                code, display = concept.get("code"), concept.get("display")
                if not code or not display:
                    continue
                entry = {"system": system, "code": str(code), "display": display}
                for alias in [display, *(concept.get("aliases") or [])]:
                    key = normalise(alias)
                    if key:
                        lookup.setdefault(key, entry)
            built[domain] = {"system": system, "lookup": lookup}
            log.info("terminology: %s — %d term(s) bound to %s",
                     domain, len(lookup), system)
        _index = built
        return _index


def reload() -> None:
    global _index
    with _lock:
        _index = None
    _load()


def lookup(domain: str, text: str, *, allow_substring: bool = False) -> Optional[dict]:
    """Return ``{system, code, display}`` for *text*, or None.

    ``allow_substring`` enables a narrow containment pass for domains where
    the dictated string reliably embeds the concept plus extra detail (a
    medication name followed by its dose). It is off by default because
    substring matching across, say, condition names invites false positives
    like "no diabetes" matching "diabetes".
    """
    idx = _load().get(domain)
    if not idx:
        return None
    key = normalise(text)
    if not key:
        return None

    hit = idx["lookup"].get(key)
    if hit:
        return dict(hit)

    if allow_substring:
        # Longest alias first, so "type 2 diabetes" beats "diabetes".
        best: Optional[tuple[int, dict]] = None
        for alias, entry in idx["lookup"].items():
            if len(alias) < 4:
                continue  # too short to be a safe substring signal
            if re.search(rf"(?:^|\s){re.escape(alias)}(?:\s|$)", key):
                if best is None or len(alias) > best[0]:
                    best = (len(alias), entry)
        if best:
            return dict(best[1])
    return None


def codeable_concept(domain: str, text: str, *,
                     allow_substring: bool = False) -> Optional[dict]:
    """A FHIR ``CodeableConcept`` for *text*.

    Coded when the term is bound, text-only when it is not. The original text
    is always preserved in ``.text`` so nothing dictated is lost to the
    mapping.
    """
    if not text:
        return None
    concept = {"text": str(text)}
    hit = resolve(domain, text, allow_substring=allow_substring)
    if hit:
        concept["coding"] = [{"system": hit["system"], "code": hit["code"],
                              "display": hit["display"]}]
    return concept


def stats() -> dict[str, int]:
    return {domain: len(spec["lookup"]) for domain, spec in _load().items()}


# ---------------------------------------------------------------------------
# Remote terminology server
# ---------------------------------------------------------------------------
# The local file is a starter set and always will be: SNOMED CT is licensed
# and far too large to vendor. A FHIR terminology server is the real answer at
# scale, and it slots in at exactly this lookup point — try the local map
# first (fast, offline, curated), then ask the server for anything unbound.
#
# Configure with TERMINOLOGY_SERVER_URL (a FHIR base URL, e.g.
# https://tx.fhir.org/r4). Results are cached in-process; the server is never
# on the critical path for a term the local file already knows.
TX_URL = os.environ.get("TERMINOLOGY_SERVER_URL", "").strip().rstrip("/")
TX_TIMEOUT = float(os.environ.get("TERMINOLOGY_SERVER_TIMEOUT_SEC", "3"))
TX_CACHE_MAX = int(os.environ.get("TERMINOLOGY_CACHE_MAX", "5000"))

_DOMAIN_SYSTEM = {
    "condition": "http://snomed.info/sct",
    "allergy": "http://snomed.info/sct",
    "medication": "http://www.nlm.nih.gov/research/umls/rxnorm",
}
_tx_cache: dict[tuple[str, str], Optional[dict]] = {}
_tx_lock = threading.Lock()


def server_enabled() -> bool:
    return bool(TX_URL)


def lookup_remote(domain: str, text: str) -> Optional[dict]:
    """Ask the configured terminology server to resolve *text*.

    Uses ``ValueSet/$expand`` with a filter, which is the standard way to ask
    "what concept matches this string" and is supported far more widely than
    the various free-text search extensions. A miss, a timeout, or an
    unreachable server all return None — the export degrades to text-only
    rather than failing, because an unavailable terminology server must not
    take the clinical pipeline down with it.
    """
    if not TX_URL:
        return None
    system = _DOMAIN_SYSTEM.get(domain)
    if not system:
        return None

    key = (domain, normalise(text))
    with _tx_lock:
        if key in _tx_cache:
            return _tx_cache[key]

    result: Optional[dict] = None
    try:
        import httpx  # noqa: PLC0415

        r = httpx.get(
            f"{TX_URL}/ValueSet/$expand",
            params={"url": f"{system}?fhir_vs", "filter": text, "count": 1},
            timeout=TX_TIMEOUT,
            headers={"Accept": "application/fhir+json"},
        )
        if r.status_code == 200:
            contains = ((r.json() or {}).get("expansion") or {}).get("contains") or []
            if contains:
                c = contains[0]
                if c.get("code") and c.get("display"):
                    result = {"system": c.get("system") or system,
                              "code": c["code"], "display": c["display"]}
        else:
            log.debug("terminology: server returned %s for %r", r.status_code, text)
    except Exception as e:  # noqa: BLE001
        log.debug("terminology: server lookup failed for %r (%s)", text, e)

    with _tx_lock:
        if len(_tx_cache) >= TX_CACHE_MAX:
            _tx_cache.clear()
        _tx_cache[key] = result
    return result


def resolve(domain: str, text: str, *, allow_substring: bool = False,
            use_server: bool = True) -> Optional[dict]:
    """Local bindings first, then the terminology server if one is configured."""
    hit = lookup(domain, text, allow_substring=allow_substring)
    if hit:
        return hit
    if use_server and TX_URL:
        return lookup_remote(domain, text)
    return None
