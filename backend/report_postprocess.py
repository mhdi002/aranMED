"""Remove unfilled template residue from a generated report.

The model is instructed (see ``_STRUCTURE_SYS``) to choose one line per group
of ``*``-prefixed alternatives, fill every placeholder, and drop conditional
lines it has no dictated content for. It usually complies. "Usually" is the
wrong reliability target for a line that tells a clinician the study was
suboptimal without saying why, or reports a measurement as a literal ``XX`` --
both read as findings rather than as leftovers.

So the prompt rule gets a deterministic backstop. The patterns live in
``data/report_postprocess.json`` rather than here, because which residue a
template can leave behind is a property of the template catalogue, not of this
code -- a site with its own templates edits data, not Python.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("report_postprocess")

_CONFIG_PATH = Path(__file__).with_name("data") / "report_postprocess.json"

_patterns: list[tuple[re.Pattern[str], str]] | None = None


def _as_bool(value: str, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _load() -> list[tuple[re.Pattern[str], str]]:
    """Compile the configured patterns once, tolerating a missing/bad file.

    A malformed config must not take report generation down: an uncleaned
    report is far better than no report, and the log line says which it is.
    """
    global _patterns
    if _patterns is not None:
        return _patterns

    compiled: list[tuple[re.Pattern[str], str]] = []
    try:
        raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        for entry in raw.get("drop_line_patterns") or []:
            pattern = entry.get("pattern")
            if not pattern:
                continue
            try:
                compiled.append((re.compile(pattern, re.I), entry.get("why", "")))
            except re.error as e:
                log.warning("report_postprocess: skipping bad pattern %r: %s",
                            pattern, e)
    except FileNotFoundError:
        log.warning("report_postprocess: %s not found; cleanup disabled",
                    _CONFIG_PATH)
    except (OSError, ValueError) as e:
        log.warning("report_postprocess: cannot read %s (%s); cleanup disabled",
                    _CONFIG_PATH, e)

    _patterns = compiled
    return _patterns


def clean(report_text: str) -> tuple[str, list[str]]:
    """Return the report with residue lines removed, plus what was removed.

    Returns the input unchanged when the pass is disabled, no patterns are
    configured, or nothing matches.
    """
    if not report_text or not _as_bool(os.environ.get("REPORT_POSTPROCESS", ""), True):
        return report_text, []

    patterns = _load()
    if not patterns:
        return report_text, []

    kept: list[str] = []
    dropped: list[str] = []
    for line in report_text.splitlines():
        stripped = line.strip()
        if stripped and any(p.search(stripped) for p, _ in patterns):
            dropped.append(stripped)
            continue
        kept.append(line)

    if not dropped:
        return report_text, []

    # Collapse the blank runs that removing whole lines leaves behind.
    out: list[str] = []
    for line in kept:
        if not line.strip() and out and not out[-1].strip():
            continue
        out.append(line)

    log.info("report_postprocess: removed %d residue line(s)", len(dropped))
    return "\n".join(out).strip(), dropped
