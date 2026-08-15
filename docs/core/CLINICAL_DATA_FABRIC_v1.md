# Clinical Data Fabric v1

Companion docs: [`CORE_ARCHITECTURE_v1.md`](CORE_ARCHITECTURE_v1.md) · [`SYSTEM_OVERVIEW.md`](../SYSTEM_OVERVIEW.md) §11 (existing EHR behavior this unifies)

Implementation: `backend/fabric/` (typed models) + edits to `backend/store.py`, `backend/tools/ehr.py`. Migration: `scripts/migrate_ehr_json_to_sqlite.py`.

## 1. The problem this solves

Two things in this codebase are both called "the patient record," and they are not the same store:

1. **SQLite `patients` table** (`backend/db.py`, CRUD in `backend/store.py`) — `id, owner_user_id, name, language, data (JSON blob), created_at, updated_at`. This is what `POST/GET/DELETE /api/ehr/*` in `backend/routes.py` actually reads and writes (`store.upsert_patient`, `store.list_patients`, `store.get_patient`, `store.delete_patient`) — confirmed by reading `routes.py:80-125`. It is per-user scoped (`owner_user_id`) and cascade-linked to the `alerts` table.
2. **File-based store** (`backend/tools/ehr.py`) — one JSON file per patient at `backend/data/ehr/<patient_id>.json`, with **no owner field at all**. This is used exclusively by the agent tool-calling path (`BuildEHRTool`/`GetEHRTool`/`ListEHRTool`, reached via `POST /api/chat`).

A record built through the chat/agent path and a record built through the direct `POST /api/ehr/build` REST call currently land in two different places and cannot see each other. (The EHR-extraction system prompts and JSON-parsing logic are *not* duplicated — `routes.py::ehr_build` already imports `_EHR_SYS_EN`/`_EHR_SYS_FA`/`_extract_json` from `tools/ehr.py` rather than reimplementing them; only the storage layer diverges.)

A further finding: `POST /api/chat` (the endpoint that reaches the agent tool-calling path, `backend/app.py`) carries no `auth.current_user` dependency at all — unlike `routes.py`'s endpoints, it is unauthenticated at the transport level. This means `ToolContext` in the agent path never carries a real user identity today, which settles the "is `ToolContext` ever unauthenticated" open question from the original plan: it always is. The fold-in below therefore uses a configured **system-owner** account for anything persisted through the agent tool path, rather than a fallback that's rarely exercised.

## 2. Canonical store: SQLite `patients`

SQLite wins because it's already what the real, tested, user-facing REST endpoints use, it has proper per-user ownership, and it's already covered by `tests/test_auth_db_api.py`. The file-based store is the one that gets folded in, not the other way around.

## 3. The typed Fabric model

`backend/fabric/` adds typed dataclasses as a **view over the existing JSON blob shape** — this is not a schema migration, the SQLite `data` column keeps storing the same JSON it always has:

```python
@dataclass
class Patient:
    name: str | None
    age: float | None
    sex: str | None
    mrn: str | None
    weight_kg: float | None

@dataclass
class Encounter:
    date: str | None
    chief_complaint: str | None
    summary: str | None

@dataclass
class Problem:
    name: str
    status: str | None  # "active" | "resolved" | None

@dataclass
class Allergy:
    substance: str
    reaction: str | None

@dataclass
class Medication:
    name: str
    dose: str | None
    route: str | None
    frequency: str | None
    frequency_hours: float | None
    indication: str | None
    notes: str | None

@dataclass
class Vitals:
    bp: str | None
    hr: float | None
    temp_c: float | None
    spo2: float | None
    rr: float | None

@dataclass
class Lab:        # NEW — optional, unpopulated this pass
    name: str
    value: str | None
    unit: str | None
    at: str | None

@dataclass
class Imaging:     # NEW — optional, unpopulated this pass
    study: str | None
    date: str | None
    report_ref: str | None

@dataclass
class EhrRecord:
    id: str
    language: str
    created_at: float
    updated_at: float
    patient: Patient
    encounter: Encounter
    problems: list[Problem]
    allergies: list[Allergy]
    medications: list[Medication]
    vitals: Vitals
    notes: str | None
    labs: list[Lab] = field(default_factory=list)
    imaging: list[Imaging] = field(default_factory=list)
```

`labs`/`imaging` are additive, default-empty fields — no existing stored record needs a migration to remain valid, and nothing currently populates them (that's future work, tracked in [`ROADMAP.md`](ROADMAP.md) under the Clinical Data Fabric's eventual Lab/Imaging integration).

## 4. Folding `tools/ehr.py` into the canonical store

`backend/tools/ehr.py`'s storage helpers (`load_record`, `save_record`, `list_records`, `load_existing_id`) are rewritten to delegate to `backend/store.py` instead of reading/writing `backend/data/ehr/*.json` directly. The tool classes (`BuildEHRTool`, `GetEHRTool`, `ListEHRTool`) and their prompts are unchanged — only where the record physically lives changes.

`owner_user_id` resolution for the agent path uses a configured **system-owner** username (`EHR_TOOL_SYSTEM_OWNER_USERNAME`, default `"admin"`, resolved to a user id via `backend/db.py`'s `users` table and cached) — since `POST /api/chat` is unauthenticated (§1), this is not a rarely-hit fallback, it is the only path available. This mirrors the file-based store's actual current behavior: chat-built records were never per-user isolated either (no owner field existed at all), so every chat-built record already lived in one global, unauthenticated namespace keyed only by slugified patient name — routing them to one system-owner id in SQLite preserves that same effective semantics rather than introducing new behavior.

## 5. Migration of existing data

`scripts/migrate_ehr_json_to_sqlite.py` — a one-off, idempotent script (not part of the app's runtime path):

1. Read every `backend/data/ehr/*.json` file.
2. For each, call `store.upsert_patient(owner_user_id=<resolved>, data=<record minus id/timestamps>, patient_id=<id>, language=<record.language>)`.
3. Since legacy files carry no owner, `--owner-user-id` is a required CLI argument with no silent default — an operator must consciously decide who owns pre-existing chat-built records rather than the script guessing.
4. Re-running the script is safe — `upsert_patient` is keyed by `patient_id`, so a second run overwrites with the same data rather than duplicating.

## 6. What does not change

- The `alerts` table and its FK relationship to `patients` — unaffected.
- `POST /api/ehr/{id}/dose`, `POST /api/alerts/check`, `POST /api/alerts/send` — all already read through `store.py`, so they're untouched by this unification.
- Report generation, ASR, and templates — the Fabric has no relationship to the generation pipeline; `patient_id` is an optional pass-through field in `clinical_safety.enrich_report_payload` used only to log an alert, not to influence report content.
