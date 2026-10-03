"""Validate FHIR JSON with the official fhir.resources (R4B) pydantic models.

Run in a *separate interpreter* because backend/ (on the test sys.path) has
its own ``fhir.py`` module that shadows the ``fhir`` namespace package.
Usage: python fhir_validate.py <file.json>  — the file holds a list of
resources; prints JSON {"ok": n, "errors": [...]}.
"""
from __future__ import annotations

import importlib
import json
import re
import sys


def model_for(rt: str):
    mod = importlib.import_module(f"fhir.resources.R4B.{rt.lower()}")
    return getattr(mod, rt)


def main() -> None:
    resources = json.load(open(sys.argv[1], encoding="utf-8"))
    ok, errors = 0, []
    for r in resources:
        rt = r.get("resourceType")
        try:
            model_for(rt).model_validate(r)
            ok += 1
        except Exception as e:  # noqa: BLE001
            errors.append({"resourceType": rt, "id": r.get("id"),
                           "error": re.sub(r"\s+", " ", str(e))[:600]})
    print(json.dumps({"ok": ok, "errors": errors}))


if __name__ == "__main__":
    main()
