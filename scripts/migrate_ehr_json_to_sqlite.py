#!/usr/bin/env python3
"""One-off migration: fold the legacy per-patient EHR JSON files into the
canonical SQLite ``patients`` store — see docs/core/CLINICAL_DATA_FABRIC_v1.md.

Legacy layout: ``backend/data/ehr/<patient_id>.json``, one file per patient,
written by the agent tool-calling path (backend/tools/ehr.py) before it was
folded into backend/store.py. Those files carry no owner, so an explicit
``--owner-user-id`` is required — this script never guesses an owner.

Idempotent: re-running overwrites the same ``patient_id`` with the same
data rather than duplicating records (store.upsert_patient is keyed by id).

Usage:
  .venv/Scripts/python.exe scripts/migrate_ehr_json_to_sqlite.py --owner-user-id 1
  .venv/Scripts/python.exe scripts/migrate_ehr_json_to_sqlite.py --owner-user-id 1 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
DEFAULT_SOURCE = BACKEND / "data" / "ehr"

sys.path.insert(0, str(BACKEND))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--owner-user-id",
        type=int,
        required=True,
        help="users.id every migrated record is attributed to (legacy files have no owner).",
    )
    ap.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=f"Directory of legacy <patient_id>.json files (default: {DEFAULT_SOURCE}).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be migrated without writing to SQLite.",
    )
    args = ap.parse_args()

    if not args.source.is_dir():
        print(f"No legacy EHR directory at {args.source} — nothing to migrate.")
        return 0

    files = sorted(args.source.glob("*.json"))
    if not files:
        print(f"{args.source} has no *.json records — nothing to migrate.")
        return 0

    import auth
    import store

    owner = auth.get_user(args.owner_user_id)
    if owner is None:
        print(f"error: no user with id={args.owner_user_id}", file=sys.stderr)
        return 1

    migrated, failed = 0, 0
    for f in files:
        try:
            record = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"skip {f.name}: could not parse JSON ({e})")
            failed += 1
            continue
        pid = record.get("id") or f.stem
        language = record.get("language", "en")
        data = {k: v for k, v in record.items() if k not in ("id", "language")}
        name = (data.get("patient") or {}).get("name") or pid
        if args.dry_run:
            print(f"[dry-run] would migrate {pid!r} ({name!r}) -> owner_user_id={owner['id']}")
            migrated += 1
            continue
        store.upsert_patient(
            owner_user_id=owner["id"], data=data, patient_id=pid, language=language
        )
        print(f"migrated {pid!r} ({name!r}) -> owner_user_id={owner['id']}")
        migrated += 1

    print(f"\n{migrated} migrated, {failed} failed, {len(files)} total.")
    if not args.dry_run and migrated:
        print(
            "Legacy JSON files were left in place — verify via GET /api/ehr, "
            f"then remove {args.source} manually once satisfied."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
