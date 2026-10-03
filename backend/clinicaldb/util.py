"""Small shared helpers: ids, name normalisation, JSON columns."""
from __future__ import annotations

import json
import re
import unicodedata
import uuid
from typing import Any, Optional

# Arabic-script code points Persian text arrives with interchangeably;
# folding them makes "علي"/"علی" and "كريمي"/"کریمی" match.
_FOLD = str.maketrans({
    "ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "أ": "ا", "إ": "ا",
    "آ": "ا", "ؤ": "و", "‌": " ",
})
_DIACRITICS = re.compile(r"[ً-ٰٟ]")


def new_id() -> str:
    return uuid.uuid4().hex


def norm_name(value: Optional[str]) -> str:
    """Case/diacritic/script-variant-insensitive form used for matching."""
    if not value:
        return ""
    s = unicodedata.normalize("NFKC", str(value)).translate(_FOLD)
    s = _DIACRITICS.sub("", s)
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s)
                if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def norm_date(value: Optional[str]) -> Optional[str]:
    """Return ISO ``YYYY-MM-DD`` for ISO or DICOM/HL7 ``YYYYMMDD`` input."""
    if not value:
        return None
    s = str(value).strip()
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) == 6:
        return f"{digits[0:4]}-{digits[4:6]}"
    if len(digits) == 4:
        return digits
    return None


def norm_sex(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = str(value).strip().lower()
    return {
        "m": "male", "male": "male", "مرد": "male",
        "f": "female", "female": "female", "زن": "female",
        "o": "other", "other": "other",
        "u": "unknown", "unknown": "unknown",
    }.get(v, "unknown")


def jdump(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def jload(value: Optional[str], default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def row(r) -> Optional[dict]:
    return dict(r) if r is not None else None
