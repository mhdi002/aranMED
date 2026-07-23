"""Clinical safety triage + template-mismatch awareness.

Used after transcription→report and on knowledge / EHR / chat answers.
MedicalRAG is consulted over HTTP when configured; local heuristics always
run so offline / test environments still get deterministic flags.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

import report_rules
import templates as templates_mod

log = logging.getLogger("clinical_safety")

# Severity levels surfaced to the API / UI
SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_MODERATE = "moderate"
SEVERITY_LOW = "low"
SEVERITY_NONE = "none"

_CRITICAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "tension_pneumothorax",
        re.compile(
            r"\b(tension\s+pneumothorax|pneumothorax\s+under\s+tension)\b"
            r"|پنوموتوراکس\s*تحت\s*فشار",
            re.I,
        ),
    ),
    (
        "aortic_rupture_or_dissection",
        re.compile(
            r"\b(aortic\s+(rupture|transection|dissection)|ruptured\s+aorta|"
            r"type\s*[ab]\s+dissection)\b|پارگی\s*آئورت|دیسکشن\s*آئورت",
            re.I,
        ),
    ),
    (
        "active_extravasation_hemorrhage",
        re.compile(
            r"\b(active\s+(extravasation|bleeding)|life[- ]?threatening\s+"
            r"hemorrhage|massive\s+hemorrhage)\b|خونریزی\s*فعال|خونریزی\s*شدید",
            re.I,
        ),
    ),
    (
        "cerebral_herniation",
        re.compile(
            r"\b(uncal|tonsillar|transtentorial)\s+herniation\b|"
            r"\bcerebral\s+herniation\b|هرنیاسیون",
            re.I,
        ),
    ),
    (
        "large_ich_or_sah",
        re.compile(
            r"\b(large\s+(ich|intracerebral\s+hemorrhage)|"
            r"subarachnoid\s+hemorrhage\s+with\s+(hydrocephalus|herniation)|"
            r"massive\s+(ich|intracranial\s+hemorrhage))\b|"
            r"خونریزی\s*مغزی\s*وسیع",
            re.I,
        ),
    ),
    (
        "airway_compromise",
        re.compile(
            r"\b(complete\s+airway\s+obstruction|imminent\s+airway\s+loss|"
            r"critical\s+airway\s+stenosis)\b|انسداد\s*راه\s*هوایی",
            re.I,
        ),
    ),
    (
        "bowel_ischemia_or_perforation",
        re.compile(
            r"\b(free\s+intraperitoneal\s+air|pneumoperitoneum|"
            r"bowel\s+(ischemia|infarction|perforation)|"
            r"mesenteric\s+ischemia)\b|پرفوراسیون\s*روده|ایسکمی\s*روده‌?ای",
            re.I,
        ),
    ),
    (
        "pulmonary_embolism_massive",
        re.compile(
            r"\b(massive\s+(pe|pulmonary\s+embolism)|"
            r"saddle\s+(pe|embolus)|"
            r"high[- ]risk\s+pulmonary\s+embolism)\b|"
            r"آمبولی\s*ریوی\s*(وسیع|ماسیو|زین\s*اسبی)",
            re.I,
        ),
    ),
    (
        "ectopic_rupture",
        re.compile(
            r"\b(ruptured\s+ectopic|ectopic\s+pregnancy\s+with\s+rupture)\b|"
            r"بارداری\s*خارج\s*رحمی\s*پاره",
            re.I,
        ),
    ),
    (
        "lethal_or_critical_flag",
        re.compile(
            r"\b(life[- ]?threatening|lethal\s+finding|critical\s+finding|"
            r"code\s+blue|stat\s+call\s+clinician|"
            r"immediately\s+life[- ]?threatening)\b|"
            r"یافته\s*بحرانی|تهدید\s*کننده\s*حیات",
            re.I,
        ),
    ),
]

# Benign / routine phrases that should not alone trigger critical
_NEGATION_NEAR = re.compile(
    r"\b(no|without|denies|negative\s+for|ruled\s+out|absence\s+of|"
    r"not\s+(seen|identified|present))\b|بدون|ندارد|منفی",
    re.I,
)

_SPOKEN_TEMPLATE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"(?:using|use|this\s+is|report\s+(?:is|as)|template\s+(?:is|for)|"
        r"exam(?:ination)?\s*(?:is|:)|study\s*(?:is|:))\s+"
        r"([a-z0-9][a-z0-9 _\-/]{2,60})",
        re.I,
    ),
    re.compile(
        r"(?:قالب|تمپلیت|گزارش|بررسی|آزمایش)\s*(?:است|:)?\s*"
        r"([^\n.!?]{3,60})",
        re.I,
    ),
    re.compile(
        r"\b((?:ct|mri|mr|us|ultrasound|x[- ]?ray|xr|pet)\s+"
        r"(?:of\s+the\s+)?[a-z][a-z0-9 \-/]{1,40})\b",
        re.I,
    ),
]


def _use_medrag() -> bool:
    raw = (os.getenv("CLINICAL_SAFETY_USE_MEDRAG") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _window_negated(text: str, start: int, end: int, radius: int = 40) -> bool:
    left = max(0, start - radius)
    snippet = text[left:end]
    return bool(_NEGATION_NEAR.search(snippet))


def triage_local(text: str) -> list[dict[str, Any]]:
    """Fast deterministic scan for critical / lethal findings."""
    if not (text or "").strip():
        return []
    alerts: list[dict[str, Any]] = []
    for code, pat in _CRITICAL_PATTERNS:
        for m in pat.finditer(text):
            if _window_negated(text, m.start(), m.end()):
                continue
            span = m.group(0).strip()
            alerts.append(
                {
                    "code": code,
                    "severity": SEVERITY_CRITICAL,
                    "label": code.replace("_", " ").title(),
                    "excerpt": span[:160],
                    "source": "local_triage",
                    "message": (
                        f"Critical / potentially life-threatening finding "
                        f"detected ({code.replace('_', ' ')}): «{span[:80]}»"
                    ),
                }
            )
    # Deduplicate by code
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for a in alerts:
        if a["code"] in seen:
            continue
        seen.add(a["code"])
        out.append(a)
    return out


_MEDRAG_TRIAGE_PROMPT = """You are a clinical safety triage assistant for radiology.
Classify whether the following clinical text contains CRITICAL or
LIFE-THREATENING findings that require immediate clinician notification.

Reply with ONLY compact JSON (no markdown):
{{"severity":"critical|high|moderate|low|none",
 "findings":[{{"code":"snake_case","label":"...","excerpt":"...","message":"..."}}],
 "rationale":"one short sentence"}}

Text:
{text}
"""


async def triage_with_medrag(text: str) -> list[dict[str, Any]]:
    """Ask MedicalRAG to classify severity; returns critical/high alerts only."""
    if not _use_medrag() or not (text or "").strip():
        return []
    try:
        from integrations.medrag_client import MedragError, get_medrag_client
    except Exception:  # noqa: BLE001
        return []
    try:
        client = get_medrag_client()
        payload = await client.ask(
            _MEDRAG_TRIAGE_PROMPT.format(text=text[:6000]),
            specialty="radiology",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("MedRAG clinical triage unavailable: %s", e)
        return []

    answer = (payload.get("answer") or "").strip()
    parsed = _extract_json_object(answer)
    if not parsed:
        # Fall back: if MedRAG itself returned rule_alerts, surface those.
        rule_alerts = payload.get("rule_alerts") or []
        out = []
        for a in rule_alerts:
            if isinstance(a, dict):
                out.append(
                    {
                        "code": a.get("code") or "medrag_rule",
                        "severity": a.get("severity") or SEVERITY_HIGH,
                        "label": a.get("label") or "MedRAG rule alert",
                        "excerpt": (a.get("excerpt") or "")[:160],
                        "source": "medrag_rule_alerts",
                        "message": a.get("message") or str(a),
                    }
                )
            else:
                out.append(
                    {
                        "code": "medrag_rule",
                        "severity": SEVERITY_HIGH,
                        "label": "MedRAG rule alert",
                        "excerpt": "",
                        "source": "medrag_rule_alerts",
                        "message": str(a),
                    }
                )
        return [a for a in out if a["severity"] in (SEVERITY_CRITICAL, SEVERITY_HIGH)]

    severity = str(parsed.get("severity") or SEVERITY_NONE).lower()
    findings = parsed.get("findings") or []
    alerts: list[dict[str, Any]] = []
    if severity in (SEVERITY_CRITICAL, SEVERITY_HIGH) and isinstance(findings, list):
        for f in findings:
            if not isinstance(f, dict):
                continue
            alerts.append(
                {
                    "code": f.get("code") or "medrag_finding",
                    "severity": severity
                    if severity == SEVERITY_CRITICAL
                    else (f.get("severity") or severity),
                    "label": f.get("label") or "Critical finding",
                    "excerpt": (f.get("excerpt") or "")[:160],
                    "source": "medrag_triage",
                    "message": f.get("message")
                    or parsed.get("rationale")
                    or "Critical clinical finding flagged by MedicalRAG triage.",
                }
            )
        if not alerts:
            alerts.append(
                {
                    "code": "medrag_critical",
                    "severity": severity,
                    "label": "Critical finding",
                    "excerpt": "",
                    "source": "medrag_triage",
                    "message": parsed.get("rationale")
                    or "MedicalRAG classified content as critical/high severity.",
                }
            )
    return [
        a
        for a in alerts
        if str(a.get("severity", "")).lower() in (SEVERITY_CRITICAL, SEVERITY_HIGH)
    ]


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    # Strip fences
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.M)
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        return None


async def assess_critical_findings(
    *texts: str,
    use_medrag: bool | None = None,
) -> list[dict[str, Any]]:
    """Merge local + optional MedRAG triage; return critical_alerts list."""
    blob = "\n\n".join(t for t in texts if (t or "").strip())
    local = triage_local(blob)
    medrag_on = _use_medrag() if use_medrag is None else use_medrag
    remote: list[dict[str, Any]] = []
    always = (os.getenv("CLINICAL_SAFETY_ALWAYS_MEDRAG") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    confirm = (os.getenv("CLINICAL_SAFETY_CONFIRM_MEDRAG") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    # Default: local triage only (fast, deterministic). MedRAG is opt-in:
    #   CLINICAL_SAFETY_ALWAYS_MEDRAG=1  → ask on every non-trivial text
    #   CLINICAL_SAFETY_CONFIRM_MEDRAG=1 → ask only when local already flagged
    should_ask = medrag_on and len(blob.strip()) >= 40 and (
        always or (confirm and bool(local))
    )
    if should_ask:
        remote = await triage_with_medrag(blob)

    merged: dict[str, dict[str, Any]] = {}
    for a in local + remote:
        code = str(a.get("code") or a.get("message") or id(a))
        # Prefer critical over high; prefer local excerpt when both present
        prev = merged.get(code)
        if prev is None:
            merged[code] = a
        elif (
            prev.get("severity") != SEVERITY_CRITICAL
            and a.get("severity") == SEVERITY_CRITICAL
        ):
            merged[code] = a
    # Only surface critical (and high from MedRAG) to the UI banner
    out = [
        a
        for a in merged.values()
        if str(a.get("severity", "")).lower()
        in (SEVERITY_CRITICAL, SEVERITY_HIGH)
    ]
    return out


# ---------------------------------------------------------------------------
# Template mismatch / naming-rule checks
# ---------------------------------------------------------------------------
def _slugify_loose(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def _template_catalog() -> list[dict[str, str]]:
    """id + display name + official title for every known template."""
    items: list[dict[str, str]] = []
    try:
        listed = templates_mod.list_templates()
    except Exception:  # noqa: BLE001
        listed = []
    for t in listed:
        tid = str(t.get("id") or "")
        name = str(t.get("name") or tid)
        official = report_rules.official_title_for(tid) or name
        items.append({"id": tid, "name": name, "official_title": official})
    # Also include official_titles keys that may not yet be on disk
    rules = report_rules.load_report_rules()
    for tid, title in (rules.get("official_titles") or {}).items():
        if not any(x["id"] == tid for x in items):
            items.append(
                {"id": str(tid), "name": str(title), "official_title": str(title)}
            )
    return items


def extract_spoken_template(transcript: str) -> Optional[str]:
    """Best-effort extraction of a spoken exam / template name."""
    if not (transcript or "").strip():
        return None
    # Prefer an explicit "using template …" / "exam is …" mention
    for pat in _SPOKEN_TEMPLATE_PATTERNS:
        m = pat.search(transcript)
        if m:
            spoken = m.group(1).strip(" .:;-,\"'")
            # Truncate at sentence junk
            spoken = re.split(r"[,.;\n]", spoken)[0].strip()
            if 3 <= len(spoken) <= 60:
                return spoken
    return None


def _titles_match(a: str, b: str) -> bool:
    na, nb = report_rules._norm(a), report_rules._norm(b)  # noqa: SLF001
    if not na or not nb:
        return False
    if na == nb:
        return True
    if _slugify_loose(a) == _slugify_loose(b):
        return True
    # substring containment for "CT Chest" vs "ct chest without contrast"
    if na in nb or nb in na:
        # Require meaningful overlap (≥ 2 tokens or short exact-ish)
        ta, tb = set(na.split()), set(nb.split())
        if len(ta & tb) >= 2:
            return True
    return False


def assess_template_mismatch(
    transcript: str,
    selected_template_id: str,
    *,
    selected_official_title: str | None = None,
) -> dict[str, Any]:
    """Compare spoken exam name vs UI-selected template; check naming rules.

    Selected UI template always wins for structure — this only *flags*
    mismatches / forbidden informal titles.
    """
    catalog = _template_catalog()
    selected = next(
        (c for c in catalog if c["id"] == selected_template_id),
        {
            "id": selected_template_id,
            "name": selected_template_id,
            "official_title": selected_official_title
            or report_rules.official_title_for(selected_template_id)
            or selected_template_id,
        },
    )
    if selected_official_title:
        selected = {**selected, "official_title": selected_official_title}

    spoken = extract_spoken_template(transcript)
    naming_violations: list[dict[str, Any]] = []
    mismatch = False
    spoken_resolved: str | None = None
    matched_template_id: str | None = None

    if spoken:
        # Alias → official
        aliased = report_rules.resolve_alias(spoken)
        spoken_resolved = aliased or spoken
        if report_rules.is_forbidden_title(spoken) or (
            aliased is None and report_rules.is_forbidden_title(spoken_resolved)
        ):
            naming_violations.append(
                {
                    "type": "forbidden_title",
                    "spoken": spoken,
                    "message": (
                        f"Spoken title «{spoken}» is not an approved insurance "
                        f"exam name. Use the exact official title."
                    ),
                }
            )
        # Does spoken refer to a *different* catalog template than selected?
        for c in catalog:
            if _titles_match(spoken_resolved, c["official_title"]) or _titles_match(
                spoken_resolved, c["name"]
            ) or _titles_match(spoken_resolved, c["id"].replace("_", " ")):
                matched_template_id = c["id"]
                break
        if matched_template_id and matched_template_id != selected_template_id:
            mismatch = True
        elif matched_template_id is None and not _titles_match(
            spoken_resolved, selected["official_title"]
        ) and not _titles_match(spoken_resolved, selected["name"]):
            # Spoken something that doesn't match selected — soft mismatch
            # only when it looks like a modality+bodypart phrase
            if re.search(
                r"\b(ct|mri|mr|us|ultrasound|x-?ray|pet)\b", spoken_resolved, re.I
            ):
                mismatch = True

        # Exact official-title enforcement for the *selected* template
        official = selected.get("official_title") or ""
        if official and spoken and not _titles_match(spoken, official):
            # If they spoke a near-miss forbidden informal name for this exam
            if report_rules.is_forbidden_title(spoken):
                naming_violations.append(
                    {
                        "type": "official_title_required",
                        "spoken": spoken,
                        "required": official,
                        "message": (
                            f"Insurance naming rule: use exact title "
                            f"«{official}», not «{spoken}»."
                        ),
                    }
                )

    meta = {
        "selected_template_id": selected_template_id,
        "selected_official_title": selected.get("official_title"),
        "spoken_template": spoken,
        "spoken_resolved": spoken_resolved,
        "matched_template_id": matched_template_id,
        "mismatch": mismatch,
        "naming_violations": naming_violations,
        "selected_wins": True,
        "message": None,
    }
    if mismatch:
        meta["message"] = (
            f"Spoken exam/template «{spoken}» differs from selected UI template "
            f"«{selected_template_id}» "
            f"({selected.get('official_title')}). "
            f"Selected template structure was used."
        )
    elif naming_violations and not meta["message"]:
        meta["message"] = naming_violations[0]["message"]
    return meta


STRUCTURE_SYS_EXTRA = """
TEMPLATE AUTHORITY (mandatory):
  • The TEMPLATE block is the UI-selected scaffold and ALWAYS wins for
    section structure and headings — even if the dictation names a different
    exam/template.
  • Fill ONLY the selected template's sections. Do not switch templates.
  • Preserve the template's headings and official EXAM title exactly
    (insurance naming compliance). Do not rename the exam.
  • If the dictation mentions a different study name, still use the selected
    template title/headings; place any mismatched spoken name only in
    CLINICAL HISTORY if clinically relevant, never as the EXAM line.
"""


def structure_system_prompt(base: str) -> str:
    return (base or "").rstrip() + "\n" + STRUCTURE_SYS_EXTRA.strip() + "\n"


async def enrich_report_payload(
    *,
    transcript: str,
    report_text: str,
    template_id: str,
    patient_id: str | None = None,
    owner_user_id: int | None = None,
    use_medrag: bool | None = None,
) -> dict[str, Any]:
    """Build critical_alerts + template_mismatch metadata for API responses."""
    mismatch = assess_template_mismatch(transcript, template_id)
    alerts = await assess_critical_findings(
        transcript, report_text, use_medrag=use_medrag
    )

    # Optionally persist clinical finding alerts on the EHR alert channel
    if alerts and patient_id and owner_user_id is not None:
        try:
            import store

            body = "\n".join(
                f"[{a.get('severity')}] {a.get('message')}" for a in alerts
            )
            store.log_alert(
                patient_id=patient_id,
                channel="clinical",
                recipient=f"user:{owner_user_id}",
                body=body,
                dry_run=True,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("could not log clinical alert to EHR channel: %s", e)

    return {
        "critical_alerts": alerts,
        "template_mismatch": mismatch,
    }


async def enrich_text_payload(
    text: str,
    *,
    use_medrag: bool | None = None,
) -> list[dict[str, Any]]:
    """critical_alerts for knowledge / chat / EHR-ask style endpoints."""
    return await assess_critical_findings(text, use_medrag=use_medrag)
