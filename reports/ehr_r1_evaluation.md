# EHR-R1-1.7B — Technical Fit Evaluation (test only, not integrated)

Requested evaluation of [MAGIC-AI4Med/EHR-R1](https://github.com/MAGIC-AI4Med/EHR-R1) / [BlueZeros/EHR-R1-1.7B](https://huggingface.co/BlueZeros/EHR-R1-1.7B) as a replacement for the `core` LLM role. Per user decision, this was a technical fit test only — nothing was wired into the application, and the default core model (`qwen3.5-9b` via Ollama) is unchanged.

## Blockers found before any testing

- **License: CC-BY-NC-4.0 — non-commercial only.** Disqualifying on its own for a commercial clinical product; would require separate permission from the authors regardless of technical fit.
- **No documented tool/function-calling support.** Checked the GitHub README, HF model card, and raw README source directly — no mention of `tools`, `tool_calls`, or agent capability anywhere.

## Technical fit test

Base: Qwen3-1.7B, fine-tuned on EHR-Ins (3.5M cases, MIMIC-IV-derived, 42 EHR reasoning tasks). Loaded via `transformers` on CPU (isolated from the live GPU stack), tested against the three real prompts our `core` role actually uses.

| Test | Real prompt used | Result |
|---|---|---|
| Tool calling | `backend/providers/ollama.py`'s exact `tools=` wire shape + a real tool (`get_template`) | **FAIL** — chat template supports `<tool_call>` (inherited from Qwen3 base), but the model never emitted one; started writing `# Retrieved Knowledge #` instead |
| Report structuring | `backend/tools/builtin.py::_STRUCTURE_SYS_BASE`, real chest template, real transcript (tension pneumothorax) | **FAIL** — output truncated to `CHEST SOFT TISSUE SONO` (11 chars), no findings, didn't mention the critical finding it was given |
| EHR JSON extraction | `backend/tools/ehr.py::_EHR_SYS_EN` shape, one-line patient note | **FAIL** — fabricated a complete fictitious MIMIC-IV-style admission record (invented triage vitals, a lab panel with specific values) with no basis in the input; no valid JSON produced |

Full raw model outputs and the eval script are in `scripts/../ehr_r1_eval` (scratch, not part of the repo) — available on request; results summarized here are complete, nothing omitted.

## Conclusion

Does not fit our need. The model is heavily specialized to one input/output shape (MIMIC-style EHR event logs → structured clinical reasoning) that actively conflicts with what the `core` role needs: free-form dictation → report, arbitrary patient text → schema-compliant JSON, and OpenAI-style tool invocation. The EHR JSON test result — fabricating vitals and lab values not present in the input — is a safety-relevant failure mode, not just a formatting mismatch. The 8B/72B variants share the same training recipe and license and would not be expected to differ on the license or tool-calling findings.

No code changes were made to the application. `backend/models.yaml` / `backend/models.docker.yaml` are unchanged; `core` default remains `qwen3.5-9b`.
