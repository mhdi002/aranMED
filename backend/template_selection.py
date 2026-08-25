"""Automatic report-template selection from dictation content.

No template-specific keywords are hardcoded: every ranking signal is
derived at runtime from (a) the transcript itself and (b) each template's
own title/official-title/body text, loaded through templates.py. Adding
template #57 requires zero code changes here.

Two tiers, reusing the exact logic clinical_safety.assess_template_mismatch
already uses to detect the *opposite* problem (a spoken exam that doesn't
match a manually-selected template):
  1. Explicit spoken phrase ("using template X", "exam is X") resolved
     against the official title / alias catalog -- highest confidence.
  2. Generic token-overlap ranking of every known template's title + body
     text against the transcript -- used when no explicit phrase is found,
     or to fill out the ranked candidate list.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

import report_rules
import templates as templates_mod
from clinical_safety import extract_spoken_template, template_catalog, titles_match

_STOPWORDS = {
    "the", "and", "with", "without", "for", "are", "was", "were", "this",
    "that", "there", "seen", "noted", "normal", "sign", "signs", "study",
    "patient", "will", "perform", "using", "template", "exam", "report",
    "findings", "impression", "history", "clinical", "please", "today",
}
_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")


def _tokens(text: str) -> set[str]:
    words = [t.lower() for t in _TOKEN_RE.findall(text or "")]
    toks = {w for w in words if w not in _STOPWORDS}
    # ASR/dictation often splits compound medical terms that a template's
    # title spells as one word (e.g. "hepato biliary" vs "Hepatobiliary").
    # Index adjacent-word joins too, symmetrically for every text this is
    # called on, so compound overlap works regardless of which side split it.
    for a, b in zip(words, words[1:]):
        joined = a + b
        if joined not in _STOPWORDS:
            toks.add(joined)
    return toks


@dataclass
class TemplateSuggestion:
    template_id: str
    title: str
    confidence: float  # 0..1
    matched_on: str  # "spoken_phrase" | "keyword_overlap"
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "template_id": self.template_id,
            "title": self.title,
            "confidence": self.confidence,
            "matched_on": self.matched_on,
            "detail": self.detail,
        }


def _explicit_phrase_match(transcript: str) -> TemplateSuggestion | None:
    spoken = extract_spoken_template(transcript)
    if not spoken:
        return None
    resolved = report_rules.resolve_alias(spoken) or spoken
    for c in template_catalog():
        if (
            titles_match(resolved, c["official_title"])
            or titles_match(resolved, c["name"])
            or titles_match(resolved, c["id"].replace("_", " "))
        ):
            return TemplateSuggestion(
                template_id=c["id"],
                title=c["official_title"] or c["name"],
                confidence=0.95,
                matched_on="spoken_phrase",
                detail=f'spoken "{spoken}"' + (f' -> resolved "{resolved}"' if resolved != spoken else ""),
            )
    return None


_body_token_cache: dict[str, set[str]] = {}


def _template_tokens(template_id: str, title: str) -> tuple[set[str], set[str]]:
    """(title_tokens, body_tokens). Body tokens cached — template files are static on disk."""
    title_tokens = _tokens(title)
    if template_id not in _body_token_cache:
        try:
            _body_token_cache[template_id] = _tokens(templates_mod.get_template(template_id))
        except Exception:  # noqa: BLE001
            _body_token_cache[template_id] = set()
    return title_tokens, _body_token_cache[template_id]


def _keyword_overlap_ranking(transcript: str, *, top_k: int) -> list[TemplateSuggestion]:
    tt = _tokens(transcript)
    if not tt:
        return []
    title_weight = float(os.getenv("TEMPLATE_SELECT_TITLE_WEIGHT", "3.0"))
    scored: list[tuple[float, dict]] = []
    for c in template_catalog():
        title_tokens, body_tokens = _template_tokens(c["id"], c["official_title"] or c["name"])
        title_overlap = len(tt & title_tokens)
        body_overlap = len(tt & body_tokens)
        if not title_overlap and not body_overlap:
            continue
        # Deliberately NOT normalized by template length: a template with a
        # long, detailed body would otherwise score lower than a one-line
        # template purely for having more tokens in its denominator.
        score = title_overlap * title_weight + body_overlap
        scored.append((score, c))
    if not scored:
        return []
    scored.sort(key=lambda x: x[0], reverse=True)
    max_score = scored[0][0] or 1.0
    # score/max_score is 1.0 for the winner BY CONSTRUCTION, so scaling by it
    # alone gave every top candidate exactly 0.75 no matter how weak the match
    # -- which made TEMPLATE_SELECT_MIN_CONFIDENCE unable to reject anything and
    # turned auto-selection into "always pick the top keyword hit". Observed: a
    # garbled abdominal-trauma dictation confidently routed to OB Sonography at
    # 0.75, one keyword ahead of five abdominal templates at 0.60.
    #
    # Weight by how far clear of the runner-up the winner is. A template that
    # barely edges out several others has not identified the study; it has won a
    # coin toss, and the caller should be told that so it can ask for a manual
    # pick instead of filling the wrong form.
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    margin = (max_score - runner_up) / max_score if max_score else 0.0
    # A sole candidate has no runner-up to beat, so it keeps most of its score;
    # the floor stops distinctiveness from zeroing out an otherwise fine match.
    margin_floor = float(os.getenv("TEMPLATE_SELECT_MARGIN_FLOOR", "0.4"))
    distinctiveness = margin_floor + (1.0 - margin_floor) * margin
    out = []
    for score, c in scored[:top_k]:
        out.append(
            TemplateSuggestion(
                template_id=c["id"],
                title=c["official_title"] or c["name"],
                # Capped below the explicit-phrase tier so a strong spoken
                # match always outranks a content-similarity guess.
                confidence=round(
                    min(1.0, score / max_score) * 0.75 * distinctiveness, 3
                ),
                matched_on="keyword_overlap",
            )
        )
    return out


def suggest_templates(transcript: str, *, top_k: int = 3) -> list[TemplateSuggestion]:
    """Rank candidate templates for *transcript*, best first. Never raises;
    returns [] if nothing scores above zero. Caller decides any accept threshold."""
    if not (transcript or "").strip():
        return []
    out: list[TemplateSuggestion] = []
    explicit = _explicit_phrase_match(transcript)
    if explicit:
        out.append(explicit)
    for s in _keyword_overlap_ranking(transcript, top_k=top_k):
        if s.template_id not in {o.template_id for o in out}:
            out.append(s)
    return out[:top_k]


def auto_select_template(transcript: str) -> TemplateSuggestion | None:
    """Best candidate if it clears the minimum-confidence bar; else None so
    the caller falls back to requiring a manual pick. Bar is configurable
    (TEMPLATE_SELECT_MIN_CONFIDENCE), never hardcoded per template."""
    min_confidence = float(os.getenv("TEMPLATE_SELECT_MIN_CONFIDENCE", "0.15"))
    suggestions = suggest_templates(transcript, top_k=1)
    if suggestions and suggestions[0].confidence >= min_confidence:
        return suggestions[0]
    return None
