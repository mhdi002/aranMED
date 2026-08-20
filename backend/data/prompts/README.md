# System prompts

Prompt text used by the agent and the EHR builder, kept as editable data
files rather than Python string literals — the same pattern
`data/templates/` and `data/report_rules/` use for their clinical content.

| File | Used by | Purpose |
| --- | --- | --- |
| `agent_system.txt` | `backend/agent.py` | Top-level orchestration system prompt for the tool-calling agent. |
| `ehr_build_en.txt` | `backend/tools/ehr.py` | Instructs the core LLM to emit a strict-JSON EHR with English field values. |
| `ehr_build_fa.txt` | `backend/tools/ehr.py` | Same, with Persian field values. |

## Editing

Edit the `.txt` file and restart the backend (or call `prompts.reload()` in a
long-running process) — no code change or image rebuild needed. Point
`PROMPTS_DIR` at a different directory to override the whole set per
deployment.

**The JSON schema in the two `ehr_build_*` prompts is not free text.** It is
mirrored by `EHRRecordSchema` in `backend/tools/ehr.py`, which validates the
model's output before anything is persisted. If you change a field name or
type here, change it there too — otherwise valid model output will start
failing validation.

Each prompt also has an in-code fallback in its calling module: if a file is
missing or unreadable, the previously-shipped text is used and a warning is
logged, so an incomplete deployment degrades rather than breaking.
