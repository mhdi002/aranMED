"""Shared clinical database spine: portable migrations, facility registry,
Master Patient Index and the interop message log.

See docs/core/CLINICAL_DB_SCHEMA_v1.md.
"""
from clinicaldb import schema  # noqa: F401  (registers the mpi tables)
