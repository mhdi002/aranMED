"""Radiology report templates (local *.txt only).

Institutional templates live in ``backend/data/templates/`` (see
``scripts/import_report_templates.py``). Filename stem = ``template_id``.

Optional legacy radreport.org scrape is off unless ``TEMPLATES_FETCH_RADREPORT=1``.
Builtin radreport-style skeletons are not re-seeded (insurance titles must come
from the institutional library).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

from config import RADREPORT_BASE, TEMPLATES_DIR, TEMPLATES_FETCH_RADREPORT

log = logging.getLogger("templates")


def _slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower())
    return text.strip("_")[:80] or "template"


def _parse_template_file(path: Path) -> tuple[dict[str, str], str]:
    """Split optional ``# key: value`` header metadata from the clinical body."""
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body_lines: list[str] = []
    in_header = True
    for ln in raw.splitlines():
        if in_header and ln.startswith("#") and ":" in ln:
            key, _, val = ln.lstrip("#").partition(":")
            meta[key.strip().lower()] = val.strip()
            continue
        if in_header and not ln.strip():
            # blank line after header block
            in_header = False
            continue
        in_header = False
        body_lines.append(ln)
    body = "\n".join(body_lines).strip() + ("\n" if body_lines else "")
    if "title" not in meta:
        # Fall back to first non-empty body line or stem
        for ln in body_lines:
            t = ln.strip().rstrip(":").strip()
            if t:
                meta["title"] = t
                break
        else:
            meta["title"] = path.stem.replace("_", " ").title()
    meta.setdefault("id", path.stem)
    return meta, body


# ---------------------------------------------------------------------------
# Optional radreport.org scraper (disabled by default)
# ---------------------------------------------------------------------------
def _scrape_index() -> Iterable[tuple[str, str]]:
    """Yield (title, url) tuples for every public template found."""
    candidates = [
        f"{RADREPORT_BASE}/home/templates",
        f"{RADREPORT_BASE}/templates",
        f"{RADREPORT_BASE}/api/templates",
    ]
    for url in candidates:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "asr-agent/1.0"})
        except Exception as e:  # noqa: BLE001
            log.debug("index fetch failed %s (%s)", url, e)
            continue
        if r.status_code != 200:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a[href*='template']"):
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if not title or len(title) < 4:
                continue
            if href.startswith("/"):
                href = RADREPORT_BASE + href
            if href.startswith("http"):
                yield title, href
        if any(True for _ in soup.select("a[href*='template']")):
            return


def _scrape_body(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "asr-agent/1.0"})
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        node = soup.find("pre") or soup.find("article") or soup.find("main") or soup.body
        if node is None:
            return None
        text = node.get_text("\n", strip=True)
        return text if len(text) > 200 else None
    except Exception as e:  # noqa: BLE001
        log.debug("body fetch failed %s (%s)", url, e)
        return None


def refresh_from_radreport(limit: int = 25) -> int:
    """Best-effort fetch of additional templates. Returns count saved."""
    saved = 0
    for title, url in _scrape_index():
        if saved >= limit:
            break
        body = _scrape_body(url)
        if not body:
            continue
        path = TEMPLATES_DIR / f"{_slugify(title)}.txt"
        if path.exists():
            continue
        path.write_text(f"# title: {title}\n# source: {url}\n\n{body}\n", encoding="utf-8")
        saved += 1
        log.info("cached template '%s'", title)
    return saved


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def initialise() -> None:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    n_local = len(list(TEMPLATES_DIR.glob("*.txt")))
    log.info("Templates dir %s (%s local .txt)", TEMPLATES_DIR, n_local)
    if not TEMPLATES_FETCH_RADREPORT:
        return
    try:
        n = refresh_from_radreport()
        if n:
            log.info("Saved %s additional templates from radreport.org", n)
    except Exception as e:  # noqa: BLE001
        log.warning("radreport refresh skipped: %s", e)


def list_templates() -> list[dict]:
    out = []
    for p in sorted(TEMPLATES_DIR.glob("*.txt")):
        meta, body = _parse_template_file(p)
        out.append({
            "id": p.stem,
            "name": meta.get("title") or p.stem.replace("_", " ").title(),
            "size": p.stat().st_size,
            "source_name": meta.get("source_name") or "",
        })
    return out


def get_template(template_id: str) -> str:
    """Return the clinical template body (metadata headers stripped)."""
    p = TEMPLATES_DIR / f"{_slugify(template_id)}.txt"
    if not p.exists():
        # Also allow exact stem match without re-slugifying already-slug ids
        p2 = TEMPLATES_DIR / f"{template_id}.txt"
        if p2.exists():
            p = p2
        else:
            raise FileNotFoundError(template_id)
    _meta, body = _parse_template_file(p)
    return body


def get_template_meta(template_id: str) -> dict[str, str]:
    p = TEMPLATES_DIR / f"{_slugify(template_id)}.txt"
    if not p.exists():
        p2 = TEMPLATES_DIR / f"{template_id}.txt"
        if not p2.exists():
            raise FileNotFoundError(template_id)
        p = p2
    meta, _body = _parse_template_file(p)
    return meta
