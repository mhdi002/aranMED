"""Hospital / insurance report-title rules.

Primary machine-readable store: ``backend/data/REPORT_RULES.json``
(override with ``REPORT_RULES_PATH``).

Human / RAG source of truth for the full catalogue:
``REPORT_RULES_DOCX`` / sibling ``.txt`` under ``backend/data/report_rules/``.

At report time we inject a local excerpt into the LLM prompt and optionally
consult MedicalRAG (``/ask``) for naming guidance — without modifying MedRAG.
"""
from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from config import (
    REPORT_RULES_DIR,
    REPORT_RULES_DOCX,
    REPORT_RULES_MAX_CHARS,
    REPORT_RULES_MEDRAG,
    REPORT_RULES_TXT,
    ROOT,
)

log = logging.getLogger("report_rules")

_DEFAULT_JSON = ROOT / "data" / "REPORT_RULES.json"


def rules_json_path() -> Path:
    raw = (os.getenv("REPORT_RULES_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return _DEFAULT_JSON.resolve()


def rules_docx_path() -> Path:
    return Path(REPORT_RULES_DOCX).expanduser().resolve()


def rules_txt_path() -> Path:
    return Path(REPORT_RULES_TXT).expanduser().resolve()


def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s\-+/]", "", s)
    return s.strip()


def clear_rules_cache() -> None:
    load_report_rules.cache_clear()
    load_rules_text.cache_clear()


@lru_cache(maxsize=1)
def load_report_rules() -> dict[str, Any]:
    """Load JSON naming rules (official_titles / aliases / forbidden_titles)."""
    path = rules_json_path()
    if not path.is_file():
        log.warning("REPORT_RULES.json missing at %s — using empty rules", path)
        return {
            "official_titles": {},
            "aliases": {},
            "forbidden_titles": [],
            "notes": "",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to parse %s: %s", path, e)
        return {
            "official_titles": {},
            "aliases": {},
            "forbidden_titles": [],
            "notes": "",
        }
    if not isinstance(data, dict):
        return {
            "official_titles": {},
            "aliases": {},
            "forbidden_titles": [],
            "notes": "",
        }
    data.setdefault("official_titles", {})
    data.setdefault("aliases", {})
    data.setdefault("forbidden_titles", [])
    data.setdefault("notes", "")
    return data


def official_title_for(template_id: str) -> str:
    rules = load_report_rules()
    titles = rules.get("official_titles") or {}
    if template_id in titles:
        return str(titles[template_id])
    # Fall back to template file metadata
    try:
        import templates as templates_mod

        meta = templates_mod.get_template_meta(template_id)
        return str(meta.get("title") or "")
    except Exception:  # noqa: BLE001
        return ""


def resolve_alias(spoken: str) -> Optional[str]:
    """Map informal spoken title → official title string, if known."""
    rules = load_report_rules()
    aliases = rules.get("aliases") or {}
    key = _norm(spoken)
    for informal, official in aliases.items():
        if _norm(str(informal)) == key:
            return str(official)
    # Also accept exact official title as resolved-to-self
    for official in (rules.get("official_titles") or {}).values():
        if _norm(str(official)) == key:
            return str(official)
    return None


def is_forbidden_title(spoken: str) -> bool:
    rules = load_report_rules()
    key = _norm(spoken)
    for bad in rules.get("forbidden_titles") or []:
        if _norm(str(bad)) == key:
            return True
    return False


@lru_cache(maxsize=1)
def load_rules_text() -> str:
    """Full catalogue text from sibling .txt (preferred) or DOCX."""
    txt = rules_txt_path()
    if txt.is_file():
        return txt.read_text(encoding="utf-8")
    docx = rules_docx_path()
    if not docx.is_file():
        log.warning("Report rules file missing: %s / %s", txt, docx)
        return ""
    try:
        from docx import Document

        doc = Document(str(docx))
        return "\n".join(
            (p.text or "").rstrip() for p in doc.paragraphs if (p.text or "").strip()
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to read rules DOCX %s: %s", docx, e)
        return ""


def extract_official_titles(text: str | None = None) -> list[str]:
    """Heuristic: lines that look like official exam titles."""
    text = text if text is not None else load_rules_text()
    titles: list[str] = []
    for ln in text.splitlines():
        t = ln.strip()
        if not t or len(t) < 8 or len(t) > 180:
            continue
        if t.endswith(":"):
            t = t[:-1].strip()
        alpha = [c for c in t if c.isalpha()]
        if not alpha:
            continue
        upper_ratio = sum(1 for c in alpha if c.isupper()) / len(alpha)
        modality = bool(
            re.search(
                r"\b(CT|CTA|MRI|MR|US|ULTRASOUND|SONO|HRCT|SPIRAL|X-?RAY|BARIUM|VCUG|UGI)\b",
                t,
                re.I,
            )
        )
        if modality and (upper_ratio > 0.45 or "CONTRAST" in t.upper()):
            titles.append(t)
    seen: set[str] = set()
    out: list[str] = []
    for t in titles:
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def rules_excerpt_for_prompt(
    *,
    template_title: str = "",
    transcript: str = "",
    max_chars: int | None = None,
) -> str:
    """Build a prompt excerpt: matching official titles + JSON naming notes."""
    limit = int(max_chars if max_chars is not None else REPORT_RULES_MAX_CHARS)
    rules = load_report_rules()
    json_bits: list[str] = []
    notes = (rules.get("notes") or "").strip()
    if notes:
        json_bits.append(notes)
    for tid, title in (rules.get("official_titles") or {}).items():
        json_bits.append(f"  - [{tid}] {title}")
    forbidden = rules.get("forbidden_titles") or []
    if forbidden:
        json_bits.append(
            "Forbidden shortened titles: " + ", ".join(f"«{x}»" for x in forbidden)
        )
    for informal, official in (rules.get("aliases") or {}).items():
        json_bits.append(f"  alias «{informal}» → «{official}»")

    full = load_rules_text()
    titles = extract_official_titles(full) if full else []
    needles: list[str] = []
    for token in re.findall(r"[A-Za-z]{3,}", f"{template_title} {transcript}"):
        low = token.lower()
        if low not in needles and low not in {"the", "and", "with", "without", "scan"}:
            needles.append(low)
        if len(needles) >= 12:
            break

    matched: list[str] = []
    for title in titles:
        low = title.lower()
        if any(n in low for n in needles):
            matched.append(title)
        if len(matched) >= 25:
            break
    if not matched:
        matched = titles[:40]

    header = (
        "HOSPITAL/INSURANCE EXAM TITLE RULES (use EXACT titles; never shorten):\n"
        "Example: use the full official title such as «neck soft tissue ct» — "
        "do NOT invent shortened forms like «neck ct».\n"
    )
    body_parts = []
    if json_bits:
        body_parts.append("Configured rules:\n" + "\n".join(json_bits[:80]))
    if matched:
        body_parts.append(
            "Official title catalogue (subset):\n"
            + "\n".join(f"  - {t}" for t in matched)
        )
    excerpt = header + "\n\n".join(body_parts)
    if len(excerpt) > limit:
        excerpt = excerpt[: limit - 20] + "\n…(truncated)…"
    return excerpt


async def consult_medrag_naming_rules(
    *,
    template_title: str = "",
    transcript: str = "",
) -> str:
    """Ask MedicalRAG about exact exam naming; empty string if unavailable."""
    if not REPORT_RULES_MEDRAG:
        return ""
    query = (
        "Hospital/insurance radiology report naming rules: what is the exact required "
        "exam title wording for this study? Reject shortened titles "
        "(e.g. use 'neck soft tissue ct' not 'neck ct'). "
        f"Selected template title: {template_title or '(unknown)'}. "
        f"Dictation excerpt: {(transcript or '')[:400]}"
    )
    # This consult is an *optional* enhancement: the local rules excerpt is
    # already in the prompt, so a slow or cold MedicalRAG must never hold up
    # report generation. Using the shared client's MEDRAG_TIMEOUT_SEC (often
    # 1200s, sized for full RAG inference) meant a warming MedicalRAG could
    # stall /api/report for twenty minutes. Bound it separately and give up
    # quietly. See docs/core/CONFIGURATION.md.
    budget = float(os.getenv("REPORT_RULES_MEDRAG_TIMEOUT_SEC", "20"))
    try:
        import asyncio

        from integrations.medrag_client import MedragClient, get_medrag_client

        client = get_medrag_client()
        payload = await asyncio.wait_for(
            client.ask(query, specialty="radiology"), timeout=budget
        )
        return MedragClient.format_answer(payload)
    except asyncio.TimeoutError:
        log.info(
            "MedRAG naming-rules consult skipped: exceeded %.0fs budget "
            "(report proceeds with local naming rules)", budget,
        )
        return ""
    except Exception as e:  # noqa: BLE001
        log.info("MedRAG naming-rules consult skipped: %s", e)
        return ""


def build_report_context(
    *,
    template_title: str = "",
    transcript: str = "",
    extra_context: Optional[str] = None,
    medrag_answer: str = "",
) -> str:
    """Merge local rules excerpt + optional MedRAG answer + caller extra_context."""
    parts: list[str] = []
    local = rules_excerpt_for_prompt(
        template_title=template_title, transcript=transcript
    )
    if local:
        parts.append(local)
    if medrag_answer:
        parts.append("MEDRAG NAMING GUIDANCE:\n" + medrag_answer.strip())
    if extra_context and extra_context.strip():
        parts.append(extra_context.strip())
    return "\n\n".join(parts)


def rebuild_rules_json_from_templates() -> dict[str, Any]:
    """Refresh official_titles from on-disk templates; keep aliases/forbidden."""
    import templates as templates_mod

    existing = load_report_rules()
    official: dict[str, str] = dict(existing.get("official_titles") or {})
    for t in templates_mod.list_templates():
        tid = t["id"]
        official[tid] = t.get("name") or tid.replace("_", " ")
    # Preserve canonical insurance examples even if not on disk as .txt
    official.setdefault("neck_soft_tissue_ct", "neck soft tissue ct")
    data = {
        "official_titles": official,
        "aliases": existing.get("aliases")
        or {
            "neck ct": "neck soft tissue ct",
            "ct neck": "neck soft tissue ct",
            "soft tissue neck ct": "neck soft tissue ct",
        },
        "forbidden_titles": existing.get("forbidden_titles")
        or ["neck ct", "ct neck"],
        "notes": (
            existing.get("notes")
            or "Insurance/hospital naming: use exact official titles. "
            "Selected UI template always wins for report structure."
        ),
        "rules_docx": str(REPORT_RULES_DOCX),
        "rules_dir": str(REPORT_RULES_DIR),
    }
    path = rules_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    clear_rules_cache()
    return data
