"""Isolate the Persian -> English translation layer from the ASR layer.

Feeds known Persian ASR text straight to to_english_clinical() and reports
whether the numerals present in the source survive into the English. Numbers
are the part a clinician cannot re-derive, so losing one is unrecoverable.
"""
import asyncio
import re
import sys

sys.path.insert(0, "/app/backend")

from english_transcript import to_english_clinical  # noqa: E402
from registry import Registry  # noqa: E402

PERSIAN = (
    "سلام لطفاً برای بیمار احمد حوشیاری عبدو پلوی که از نظر بررسی فریفلوید "
    "تایبه فرماید که ملد انترولوپ فریفلوید است در عبدو پلوی کویتی و خط بعدیش "
    "هم اویدنس آف تو هایپوکوک استرکتر و اینترنال رویتیکولیشن و "
    "نه با سکولاریتی 50 در 50 در 50 و سی در 51 در اینکه هماتومه را قرار می کنید "
    "مطرح کردیم."
)

DIGITS = re.compile(r"\d+")


async def main():
    src_nums = DIGITS.findall(PERSIAN)
    print("source numerals :", src_nums)
    print("source chars    :", len(PERSIAN))
    print()
    for attempt in range(1, 4):
        english = await to_english_clinical(PERSIAN, registry=Registry.get())
        out_nums = DIGITS.findall(english)
        missing = [n for n in src_nums if n not in out_nums]
        print(f"--- attempt {attempt} ---")
        print("english :", english)
        print("numerals:", out_nums, "| MISSING:", missing or "none")
        print()


asyncio.run(main())
