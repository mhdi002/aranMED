"""Clinical Data Fabric — docs/core/CLINICAL_DATA_FABRIC_v1.md.

Typed dataclasses over the patient/EHR JSON blob already stored in
``backend/db.py``'s ``patients`` table (via ``backend/store.py``). This is a
typed *view*, not a schema migration — every field here already exists in
the JSON that ``store.upsert_patient``/``get_patient`` read and write.
"""
from __future__ import annotations

from .models import (
    Allergy,
    Encounter,
    EhrRecord,
    Imaging,
    Lab,
    Medication,
    Patient,
    Problem,
    Vitals,
)

__all__ = [
    "Allergy",
    "Encounter",
    "EhrRecord",
    "Imaging",
    "Lab",
    "Medication",
    "Patient",
    "Problem",
    "Vitals",
]
