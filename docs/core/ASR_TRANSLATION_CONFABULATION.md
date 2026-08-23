# Open defect: the English "transcript" is a confabulation, not a transcription

**Status:** open, unfixed. **Severity:** clinical-safety critical.
**Found:** 2026-08-23, on the deployed GPU stack, against real dictation audio.

This document exists because the failure is invisible from the outside: the
endpoint returns fluent, plausible, well-structured clinical English. It reads
like a *better* transcript than the truth. Nothing about the output signals
that most of it was invented.

---

## What happens

`/api/transcribe` runs two stages, not one:

```
audio → [1] Whisper (Triton compat)  → Persian text
      → [2] LLM translation           → English "transcript"
```

Stage 2 lives in `backend/english_transcript.py` (`to_english_clinical`) and is
triggered from `backend/providers/triton_asr.py::_ensure_english` whenever the
ASR output contains characters in the Persian Unicode range.

**Stage 2 fabricates.** It is a 4B quantized model being asked to translate
garbled domain-specific ASR output, and when the source is unclear it produces
confident clinical prose that is not in the source.

## Evidence

Sample: `Feb 3, 5.38 PM.m4a`, 41.9 s, Persian dictation.

### Stage 1 output (raw, faithful)

```
سلام لطفاً برای بیمار احمد حوشیاری عبدو پلوی که از نظر بررسی فریفلوید
تایبه فرماید که ملد انترولوپ فریفلوید است در عبدو پلوی کویتی و خط بعدیش
هم اویدنس آف تو هایپوکوک استرکتر و اینترنال رویتیکولیشن و
نه با سکولاریتی 50 در 50 در 50 و سی در 51 در اینکه هماتومه را قرار می کنید
```

Roughly: free-fluid assessment, mild interloop free fluid in the abdominopelvic
cavity, evidence of two hypoechoic structures with internal reticulation, **no
vascularity**, **50 × 50 × 50** and **30 × 51**, hematoma considered.

### Stage 2 output — three runs of the *same* input

| Run | English produced |
| --- | --- |
| 1 | "…dilated **intrahepatic portal vein**… The vessel demonstrates a 50/50/50/51% **echogenicity pattern**. **No thrombus was identified.**" |
| 2 | "…dilated intrahepatic portal vein… **non-sclerotic appearance**, with measurements recorded at 50, 50, 50, 51, **and 53 mm**." |
| 3 | "…portal vein demonstrates a **diameter of 50 mm in the first segment**, 50 mm in the second, and 51 mm in the third. **No focal lesions or masses are identified.**" |

Three mutually incompatible readings from identical input at `temperature=0.1`.
None of them is what the source says. Specifically:

- **The anatomy is invented.** The source never mentions a portal vein. It
  describes free fluid and hypoechoic structures.
- **Findings are invented.** "No thrombus", "abdominal aorta is patent", "no
  focal lesions or masses" appear in no run's source.
- **A measurement is invented.** Run 2 produced `53`, a number that does not
  occur in the Persian.
- **The negation is lost.** "نه با سکولاریتی" — *no* vascularity — becomes a
  positive vascular description.

### The whole-file run is worse

Through the full endpoint, the same audio returned:

> "Ultrasound examination of the right lobe of the liver in 15-year-old patient
> Hoshyar Abdolpouli reveals a hypoechoic area in the preumbilical region…"

Wrong organ (kidney/free fluid, not liver), invented age, altered patient name.
An earlier run in the same session produced a completely different and equally
fluent report describing hydronephrosis, nephrolithiasis, gallbladder, spleen
and pancreas — **none of which is in the audio at all.**

## Why it was not caught earlier

The 22-second sample (`Feb 3, 5.12 PM.m4a`) returns clean, coherent English
that reads as a correct transcript. It was accepted as verification that the
pipeline worked. It was never checked against the raw Persian, so a
confabulation that happened to be plausible passed as a pass.

> A fluent output is not evidence of a faithful one. Any future verification of
> this pipeline must compare stage-2 English against stage-1 raw text, not
> against expectations of what the dictation probably said.

`scripts/asr_translate_probe.py` does exactly that comparison and is the
regression check for this defect.

## Contributing factor: window size

`deploy/triton/compat_http_server.py` splits audio longer than
`WHISPER_MAX_SHORTFORM_S` into `WHISPER_CHUNK_LENGTH_S` windows. At the default
30 s — exactly Whisper's encoder frame — output degraded badly. Measured on the
41.9 s sample, joined Persian:

| Window | Joined stage-1 output |
| --- | --- |
| 30 s | 386 chars, second window began "پنج و یک" (fragmentary) |
| 20 s | 286 chars across 3 windows, but the numerals `50 … 50 … 50 … 51` survived intact |

Neither is complete. `scripts/asr_window_probe.py` reproduces this per-window
without loading a second copy of the model.

## What must not be done

**Do not import Whisper in a diagnostic script that runs inside the triton or
backend container.** A second copy of the model on an 11 GB card that already
holds vLLM plus Whisper causes `torch.OutOfMemoryError`, and pushing the card to
its limit has twice taken the entire host down — no SSH, no ICMP, requiring a
power cycle. Both probe scripts drive the *already-resident* model over HTTP for
this reason.

## Directions for a fix

None of these is implemented; they are recorded so the next attempt does not
start from zero.

1. **Do not let a general LLM translate a clinical transcript unconstrained.**
   Constrain the output to the source: require every numeral, laterality and
   negation in the source to appear in the output, and reject the result
   otherwise. `asr_translate_probe.py` already computes the numeral check.
2. **Return the raw text alongside the English.** The response schema already
   carries a `raw_transcript` field; it is currently `None`. Populating it makes
   the confabulation visible to the caller instead of hiding it.
3. **Prefer a translation-capable ASR pass over an LLM round-trip**, or a model
   substantially stronger than a 4-bit 4B for this specific step. The failure is
   a capability failure, not a prompting one — the prompt already says
   "Preserve every finding, organ, measurement and qualifier" and "Do NOT invent
   findings not present in the source", and the model disregards both.
4. **Tune `WHISPER_CHUNK_LENGTH_S` below 30 s** and re-measure. 20 s preserved
   the measurements where 30 s did not, but neither produced a complete
   transcript, so this is mitigation rather than a fix.
5. **Until stage 2 is trustworthy, consider `output_english: false`** for the
   Triton ASR slot, returning faithful Persian rather than fluent fiction. A
   transcript a clinician must translate is worse UX and better medicine.
