"""Map exam specialties to EN library coverage for OCR deduplication."""
from __future__ import annotations

from pathlib import Path

from medrag.config import LIBRARY_DIR

# exam specialty tag -> library folder name substrings
EXAM_SPEC_LIBRARY_DIRS: dict[str, list[str]] = {
    "cardio": ["cardiology"],
    "surgery": ["surgery"],
    "internal": ["internal_science"],
    "pediatrics": ["pediatrics"],
    "obgyn": ["gyn"],
    "ortho": ["orthopedics"],
    "derm": ["dermatology"],
    "psychiatry": ["psychiatry"],
    "neuro": ["neurology"],
    "patho": ["pathology"],
    "radiology": ["radiology"],
    "urology": ["urology"],
    "ent": ["ent"],
    "ophtho": ["ophtalmology", "ophthalmology"],
    "nephro": ["nephrology"],
    "gi": ["gi"],
    "pulmo": ["pulmonology"],
    "rheum": ["rheumatology"],
    "heme_onc": ["hematology", "oncology"],
    "infectious": ["infectious"],
    "pharm": ["basic_science"],
}

# Standard clinical topics where EN library duplicates FA درسنامه-style texts
LIBRARY_REDUNDANT_SPECIALTIES = set(EXAM_SPEC_LIBRARY_DIRS)

# Iran-only material — never skip OCR even if library has the specialty folder
IRAN_SPECIFIC_KEYWORDS = (
    "گایدلاین",
    "کتابچه",
    "اخلاق",
    "ایمن_سازی",
    "کمیته",
    "ملاحظات",
)


def library_folder_names() -> set[str]:
    if not LIBRARY_DIR.exists():
        return set()
    return {d.name.lower() for d in LIBRARY_DIR.iterdir() if d.is_dir()}


def library_covers_specialty(exam_spec: str) -> bool:
    folders = library_folder_names()
    for kw in EXAM_SPEC_LIBRARY_DIRS.get(exam_spec, []):
        if any(kw in f for f in folders):
            return True
    return False


def en_textbook_specialties(exam_rows: list[dict]) -> set[str]:
    """EN textbooks sitting in exam folder (same topic, extractable)."""
    return {
        r["specialty"]
        for r in exam_rows
        if r.get("language") == "en"
        and r.get("type") == "textbook"
        and not r.get("duplicate")
    }


def is_iran_specific_textbook(filename: str) -> bool:
    return any(k in filename for k in IRAN_SPECIFIC_KEYWORDS)


def fa_textbook_redundant(rec: dict, exam_rows: list[dict]) -> bool:
    """Skip FA textbook OCR when EN already covers this specialty."""
    if rec.get("language") != "fa" or rec.get("type") != "textbook":
        return False
    if is_iran_specific_textbook(rec.get("file", "")):
        return False
    spec = rec.get("specialty", "general")
    if spec in en_textbook_specialties(exam_rows):
        return True
    if spec in LIBRARY_REDUNDANT_SPECIALTIES and library_covers_specialty(spec):
        return True
    return False
