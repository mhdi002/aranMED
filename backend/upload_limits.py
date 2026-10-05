"""Bounded reads of request bodies and uploads.

``await file.read()`` / ``await request.body()`` pull the whole payload into
memory before any size check, so the only effective cap was the gateway's
body limit, multiplied by every concurrent request. These helpers stop at
the configured limit and answer 413 instead.

Limits (MB, environment): ``MAX_AUDIO_MB`` (dictation audio, default 100),
``MAX_IMAGE_MB`` (chat/vision images and single DICOM files, default 50);
PACS uploads use ``PACS_MAX_UPLOAD_MB`` (pacs/config.py).
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Request, UploadFile

_CHUNK = 1024 * 1024


def limit_mb(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def audio_limit() -> int:
    return limit_mb("MAX_AUDIO_MB", 100) * 1024 * 1024


def image_limit() -> int:
    return limit_mb("MAX_IMAGE_MB", 50) * 1024 * 1024


def _too_large(max_bytes: int) -> HTTPException:
    return HTTPException(413, f"upload exceeds the {max_bytes // (1024 * 1024)} MB limit")


async def aread(upload: UploadFile, max_bytes: int) -> bytes:
    """Read an upload from async code, refusing it once past *max_bytes*."""
    buf = bytearray()
    while True:
        chunk = await upload.read(_CHUNK)
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > max_bytes:
            raise _too_large(max_bytes)


def read(upload: UploadFile, max_bytes: int) -> bytes:
    """Same, for sync handlers (which FastAPI runs in a worker thread)."""
    buf = bytearray()
    while True:
        chunk = upload.file.read(_CHUNK)
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > max_bytes:
            raise _too_large(max_bytes)


async def abody(request: Request, max_bytes: int) -> bytes:
    """The raw request body, refused as soon as it passes *max_bytes*."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise _too_large(max_bytes)
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > max_bytes:
            raise _too_large(max_bytes)
    return bytes(buf)
