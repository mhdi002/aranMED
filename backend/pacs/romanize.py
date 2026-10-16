"""Fit outgoing DICOM text to what a remote device can display.

Many modality consoles only handle ISO_IR 100 (Latin-1) — Canon's Aquilion
line, for instance, supports Latin-1 and Japanese but not UTF-8. A Persian
patient name sent to them as UTF-8 shows up as garbage on the worklist, or
the device rejects the response. For such nodes (``charset`` set to a
single-byte character set, optionally ``name_romanize``) responses are
re-encoded here:

* Person names are transliterated to Latin letters (deterministic,
  letter-by-letter, so the same name always gives the same string an
  operator can match against the request form); when the node's character
  set can carry the original (ISO_IR 192), it is kept as the PN ideographic
  component (``Romanised=اصلی``).
* Any other text that the character set cannot encode is transliterated if
  it is Persian/Arabic, otherwise unencodable characters become ``?`` — a
  response is never dropped because of one character.
"""
from __future__ import annotations

from typing import Optional

from pydicom.charset import python_encoding
from pydicom.dataset import Dataset

_MAP = {
    "ا": "a", "آ": "a", "أ": "a", "إ": "e", "ء": "'", "ئ": "y", "ؤ": "o",
    "ب": "b", "پ": "p", "ت": "t", "ث": "s", "ج": "j", "چ": "ch", "ح": "h",
    "خ": "kh", "د": "d", "ذ": "z", "ر": "r", "ز": "z", "ژ": "zh", "س": "s",
    "ش": "sh", "ص": "s", "ض": "z", "ط": "t", "ظ": "z", "ع": "'", "غ": "gh",
    "ف": "f", "ق": "gh", "ک": "k", "ك": "k", "گ": "g", "ل": "l", "م": "m",
    "ن": "n", "و": "v", "ه": "h", "ة": "h", "ی": "y", "ي": "y", "ى": "a",
    "‌": "", "ً": "", "ٌ": "", "ٍ": "", "َ": "",
    "ُ": "", "ِ": "", "ّ": "", "ْ": "", "ٰ": "",
    "،": ",", "؛": ";", "؟": "?", "ـ": "",
}
_DIGITS = {**{chr(0x06F0 + i): str(i) for i in range(10)},
           **{chr(0x0660 + i): str(i) for i in range(10)}}
_VOWEL_LETTERS = set("اآوی")
# Common given names and surnames in their usual English spelling; anything
# else is transliterated letter by letter.
_COMMON = {
    "محمد": "Mohammad", "علی": "Ali", "رضا": "Reza", "حسین": "Hossein", "حسن": "Hassan",
    "مهدی": "Mehdi", "احمد": "Ahmad", "محمود": "Mahmoud", "مصطفی": "Mostafa",
    "امیر": "Amir", "سعید": "Saeed", "مجید": "Majid", "جواد": "Javad", "حمید": "Hamid",
    "عباس": "Abbas", "اکبر": "Akbar", "اصغر": "Asghar", "ابراهیم": "Ebrahim",
    "اسماعیل": "Esmaeil", "یوسف": "Yousef", "کریم": "Karim", "مسعود": "Masoud",
    "محسن": "Mohsen", "سجاد": "Sajjad", "صادق": "Sadegh", "جعفر": "Jafar",
    "فاطمه": "Fatemeh", "زهرا": "Zahra", "مریم": "Maryam", "زینب": "Zeinab",
    "سارا": "Sara", "نرگس": "Narges", "معصومه": "Masoumeh", "لیلا": "Leila",
    "مینا": "Mina", "نازنین": "Nazanin", "الهام": "Elham", "سمیرا": "Samira",
    "شیرین": "Shirin", "پریسا": "Parisa", "نسرین": "Nasrin", "طاهره": "Tahereh",
    "اسماعیلی": "Esmaeili", "محمدی": "Mohammadi", "حسینی": "Hosseini",
    "احمدی": "Ahmadi", "رضایی": "Rezaei", "کریمی": "Karimi", "موسوی": "Mousavi",
    "جعفری": "Jafari", "رحیمی": "Rahimi", "قاسمی": "Ghasemi", "صادقی": "Sadeghi",
    "حیدری": "Heydari", "کاظمی": "Kazemi", "عباسی": "Abbasi", "اکبری": "Akbari",
    "نوری": "Nouri", "هاشمی": "Hashemi", "ابراهیمی": "Ebrahimi", "یوسفی": "Yousefi",
    "تهرانی": "Tehrani", "شیرازی": "Shirazi", "اصفهانی": "Esfahani",
}
_PN_KEYS = {"PN"}
_TEXT_VRS = {"PN", "LO", "SH", "LT", "ST", "UT", "UC"}


def _is_arabic_script(ch: str) -> bool:
    return "؀" <= ch <= "ۿ" or ch in ("‌",)


def has_arabic_script(text: str) -> bool:
    return any(_is_arabic_script(c) for c in text or "")


def _word(word: str) -> str:
    known = _COMMON.get(word.replace("ي", "ی").replace("ك", "ک"))
    if known:
        return known
    out = []
    n = len(word)
    for i, ch in enumerate(word):
        if ch in _DIGITS:
            out.append(_DIGITS[ch])
            continue
        prev = word[i - 1] if i else ""
        nxt = word[i + 1] if i + 1 < n else ""
        if ch == "و":
            # Consonant at the start of a word or after a vowel letter,
            # otherwise the long vowel o/u ("حسین", "محمود").
            out.append("v" if i == 0 or prev in _VOWEL_LETTERS else "o")
        elif ch in ("ی", "ي"):
            if i == 0 or (prev in _VOWEL_LETTERS and nxt):
                out.append("y")
            else:
                out.append("i")
        elif ch == "ع" and i == 0:
            out.append("a")
        elif ch == "ه" and i == n - 1 and i > 0:
            out.append("eh")  # silent final he: "فاطمه" -> "fatemeh"
        else:
            out.append(_MAP.get(ch, ch))
    s = "".join(out)
    return s[:1].upper() + s[1:]


def romanize(text: Optional[str]) -> str:
    """Latin transliteration of Persian/Arabic text; other text unchanged.
    DICOM PN component separators (``^``) and spaces are preserved."""
    if not text:
        return ""
    if not has_arabic_script(text):
        return text
    parts = []
    for comp in str(text).split("^"):
        parts.append(" ".join(_word(w) if has_arabic_script(w) else w for w in comp.split(" ")))
    return "^".join(parts)


def _codec(charset: str) -> str:
    import codecs
    first = (charset or "ISO_IR 192").split("\\")[0].strip() or "ISO_IR 6"
    try:
        return codecs.lookup(python_encoding.get(first, "utf-8")).name
    except LookupError:
        return "utf-8"


def _encodable(text: str, codec: str) -> bool:
    try:
        text.encode(codec)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def fit_text(text: str, charset: str) -> str:
    codec = _codec(charset)
    if _encodable(text, codec):
        return text
    t = romanize(text) if has_arabic_script(text) else text
    return t.encode(codec, errors="replace").decode(codec)


def fit_person_name(value: str, charset: str, romanize_names: bool) -> str:
    value = str(value or "")
    if not value or not has_arabic_script(value):
        return fit_text(value, charset) if value else value
    codec = _codec(charset)
    if romanize_names:
        latin = romanize(value.split("=")[0])
        if codec == "utf-8":
            # Alphabetic group in Latin, original kept as the ideographic group.
            return f"{latin}={value.split('=')[0]}"
        return fit_text(latin, charset)
    return fit_text(value, charset)


def fit_dataset(ds: Dataset, charset: Optional[str], romanize_names: bool = False) -> Dataset:
    """Set Specific Character Set and make every text value encodable in it
    (recursing into sequences)."""
    # A romanising node with no explicit character set is a Latin-1 device.
    cs = (charset or ("ISO_IR 100" if romanize_names else "ISO_IR 192")).strip()

    def walk(d: Dataset) -> None:
        for elem in d:
            if elem.VR == "SQ":
                for item in elem.value or []:
                    walk(item)
                continue
            if elem.VR not in _TEXT_VRS or elem.value in (None, ""):
                continue
            if elem.VM > 1:
                elem.value = [fit_person_name(str(v), cs, romanize_names) if elem.VR in _PN_KEYS
                              else fit_text(str(v), cs) for v in elem.value]
            elif elem.VR in _PN_KEYS:
                elem.value = fit_person_name(str(elem.value), cs, romanize_names)
            else:
                elem.value = fit_text(str(elem.value), cs)

    walk(ds)
    ds.SpecificCharacterSet = cs
    return ds
