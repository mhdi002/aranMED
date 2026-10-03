"""multipart/related encode/decode for DICOMweb (PS3.18 §8.6)."""
from __future__ import annotations

import re
import secrets
from typing import Iterable, Optional


def boundary() -> str:
    return "DICOMwebBoundary" + secrets.token_hex(12)


def encode(parts: Iterable[tuple[bytes, str]], bnd: str,
           locations: Optional[Iterable[Optional[str]]] = None) -> bytes:
    out = bytearray()
    locs = list(locations) if locations is not None else None
    for i, (data, ctype) in enumerate(parts):
        out += f"--{bnd}\r\nContent-Type: {ctype}\r\n".encode()
        if locs and i < len(locs) and locs[i]:
            out += f"Content-Location: {locs[i]}\r\n".encode()
        out += f"Content-Length: {len(data)}\r\n\r\n".encode()
        out += data
        out += b"\r\n"
    out += f"--{bnd}--\r\n".encode()
    return bytes(out)


def content_type(bnd: str, part_type: str, extra: str = "") -> str:
    return f'multipart/related; type="{part_type}"; boundary={bnd}{extra}'


_BOUNDARY_RE = re.compile(r'boundary="?([^";,]+)"?', re.IGNORECASE)


def parse_boundary(content_type_header: str) -> Optional[str]:
    m = _BOUNDARY_RE.search(content_type_header or "")
    return m.group(1).strip() if m else None


def decode(body: bytes, bnd: str) -> list[tuple[dict[str, str], bytes]]:
    """Split a multipart body into ``(headers, payload)`` pairs."""
    delim = b"--" + bnd.encode()
    parts = []
    for chunk in body.split(delim)[1:]:
        if chunk.startswith(b"--"):
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        head, sep, payload = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = {}
        for line in head.decode("latin-1").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        parts.append((headers, payload))
    return parts
