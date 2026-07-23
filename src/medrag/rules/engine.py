"""Deterministic clinical rule engine (LLM explains; rules decide)."""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict


@dataclass
class RuleAlert:
    rule_id: str
    severity: str  # info | warning | critical
    message: str
    message_fa: str | None = None
    data: dict | None = None


def _num(text: str, patterns: list[str]) -> float | None:
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def check_allergy(query: str) -> list[RuleAlert]:
    alerts = []
    if re.search(r"allerg(?:y|ic).*penicillin|حساسیت.*پنی‌?سیلین", query, re.I):
        if re.search(r"amoxicillin|ampicillin|آموکسی", query, re.I):
            alerts.append(RuleAlert(
                "allergy_beta_lactam", "critical",
                "Possible beta-lactam allergy conflict with prescribed penicillin-class drug.",
                "احتمال تداخل آلرژی بتا-لاکتام با داروی پنی‌سیلینی.",
            ))
    return alerts


def egfr_metformin_warning(query: str) -> list[RuleAlert]:
    egfr = _num(query, [
        r"egfr\s*[:=]?\s*(\d+(?:\.\d+)?)",
        r"eGFR\s*[:=]?\s*(\d+(?:\.\d+)?)",
        r"میزان\s*تصفیه[^\d]*(\d+(?:\.\d+)?)",
    ])
    has_met = bool(re.search(r"metformin|متفورمین", query, re.I))
    if has_met and egfr is not None and egfr < 30:
        return [RuleAlert(
            "metformin_egfr_lt30", "critical",
            f"Metformin is contraindicated when eGFR < 30 (observed eGFR={egfr}).",
            f"متفورمین در eGFR کمتر از ۳۰ ممنوع است (eGFR مشاهده‌شده={egfr}).",
            {"egfr": egfr},
        )]
    if has_met and egfr is not None and egfr < 45:
        return [RuleAlert(
            "metformin_egfr_lt45", "warning",
            f"Review metformin dose/continuation when eGFR < 45 (observed eGFR={egfr}).",
            f"در eGFR کمتر از ۴۵ دوز/ادامه متفورمین را بازبینی کنید (eGFR={egfr}).",
            {"egfr": egfr},
        )]
    return []


def warfarin_amiodarone(query: str) -> list[RuleAlert]:
    if re.search(r"warfarin|وارفارین", query, re.I) and re.search(r"amiodarone|آمیودارون", query, re.I):
        return [RuleAlert(
            "warfarin_amiodarone", "warning",
            "Warfarin–Amiodarone interaction: expect increased INR; monitor closely.",
            "تداخل وارفارین–آمیودارون: احتمال افزایش INR؛ پایش دقیق لازم است.",
        )]
    return []


def sepsis_news2_hint(query: str) -> list[RuleAlert]:
    if re.search(r"\bsepsis\b|سپسیس|NEWS2|qSOFA", query, re.I):
        return [RuleAlert(
            "sepsis_screen", "info",
            "Consider formal sepsis screening (qSOFA/NEWS2) and local protocol.",
            "غربالگری رسمی سپسیس (qSOFA/NEWS2) و پروتکل محلی را در نظر بگیرید.",
        )]
    return []


RULES = [
    check_allergy,
    egfr_metformin_warning,
    warfarin_amiodarone,
    sepsis_news2_hint,
]


def evaluate(query: str) -> list[dict]:
    out: list[RuleAlert] = []
    for fn in RULES:
        try:
            out.extend(fn(query))
        except Exception:
            continue
    return [asdict(a) for a in out]
