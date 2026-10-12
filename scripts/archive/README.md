# Archived one-off scripts

Experiments and probes kept for reference (ASR window/segmentation trials,
an in-place truncation check, a clip-duration helper). Nothing in the
application, tests, Makefile or docs runs them, and they are not maintained.
They expect to be run from `scripts/` (some import `scripts/_endpoints.py`):

    cd scripts && PYTHONPATH=. python archive/<name>.py
