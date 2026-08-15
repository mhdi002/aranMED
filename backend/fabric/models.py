"""Clinical Data Fabric typed models (docs/core/CLINICAL_DATA_FABRIC_v1.md §3).

Every dataclass here maps 1:1 onto the JSON shape that already exists in
``patients.data`` (see ``backend/db.py``) and that ``backend/tools/ehr.py``'s
LLM extraction prompts already produce. ``labs``/``imaging`` are additive,
default-empty fields — no existing stored record needs a migration to stay
valid, and nothing populates them yet (see docs/core/ROADMAP.md).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


def _from_dict(cls, d: dict[str, Any] | None):
    d = d or {}
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class Patient:
    name: str | None = None
    age: float | None = None
    sex: str | None = None
    mrn: str | None = None
    weight_kg: float | None = None


@dataclass
class Encounter:
    date: str | None = None
    chief_complaint: str | None = None
    summary: str | None = None


@dataclass
class Problem:
    name: str
    status: str | None = None  # "active" | "resolved" | None


@dataclass
class Allergy:
    substance: str
    reaction: str | None = None


@dataclass
class Medication:
    name: str
    dose: str | None = None
    route: str | None = None
    frequency: str | None = None
    frequency_hours: float | None = None
    indication: str | None = None
    notes: str | None = None


@dataclass
class Vitals:
    bp: str | None = None
    hr: float | None = None
    temp_c: float | None = None
    spo2: float | None = None
    rr: float | None = None


@dataclass
class Lab:
    """Not yet populated by any pipeline — see docs/core/ROADMAP.md."""

    name: str
    value: str | None = None
    unit: str | None = None
    at: str | None = None


@dataclass
class Imaging:
    """Not yet populated by any pipeline — see docs/core/ROADMAP.md."""

    study: str | None = None
    date: str | None = None
    report_ref: str | None = None


@dataclass
class EhrRecord:
    id: str
    language: str
    created_at: float
    updated_at: float
    patient: Patient = field(default_factory=Patient)
    encounter: Encounter = field(default_factory=Encounter)
    problems: list[Problem] = field(default_factory=list)
    allergies: list[Allergy] = field(default_factory=list)
    medications: list[Medication] = field(default_factory=list)
    vitals: Vitals = field(default_factory=Vitals)
    notes: str | None = None
    labs: list[Lab] = field(default_factory=list)
    imaging: list[Imaging] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EhrRecord":
        d = d or {}
        return cls(
            id=d.get("id", ""),
            language=d.get("language", "en"),
            created_at=d.get("created_at", 0.0),
            updated_at=d.get("updated_at", 0.0),
            patient=_from_dict(Patient, d.get("patient")),
            encounter=_from_dict(Encounter, d.get("encounter")),
            problems=[_from_dict(Problem, p) for p in d.get("problems", [])],
            allergies=[_from_dict(Allergy, a) for a in d.get("allergies", [])],
            medications=[_from_dict(Medication, m) for m in d.get("medications", [])],
            vitals=_from_dict(Vitals, d.get("vitals")),
            notes=d.get("notes"),
            labs=[_from_dict(Lab, x) for x in d.get("labs", [])],
            imaging=[_from_dict(Imaging, x) for x in d.get("imaging", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
