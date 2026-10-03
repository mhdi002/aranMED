"""Pixel access for WADO-RS frames / rendered / thumbnail.

* ``frame_bytes`` returns one frame exactly as stored — native pixels for
  uncompressed transfer syntaxes, the encapsulated fragment(s) for
  compressed ones — which is what WADO-RS ``/frames`` serves and what the
  browser viewer decodes itself.
* ``render`` produces a display PNG/JPEG with modality LUT (rescale) and VOI
  windowing applied, either the file's own window or one the caller passes.
"""
from __future__ import annotations

import io
from typing import Optional

import numpy as np


def load(data: bytes, *, pixels: bool = True):
    import pydicom
    return pydicom.dcmread(io.BytesIO(data), force=True, stop_before_pixels=not pixels)


def frame_count(ds) -> int:
    try:
        return int(getattr(ds, "NumberOfFrames", 1) or 1)
    except (TypeError, ValueError):
        return 1


def is_compressed(ds) -> bool:
    ts = getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", None)
    return bool(ts) and ts.is_compressed


def transfer_syntax(ds) -> str:
    ts = getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", None)
    return str(ts) if ts else "1.2.840.10008.1.2.1"


def frame_bytes(ds, number: int) -> bytes:
    """Frame *number* (1-based) as stored."""
    n = frame_count(ds)
    if number < 1 or number > n:
        raise IndexError(f"frame {number} out of range 1..{n}")
    if "PixelData" not in ds:
        raise ValueError("no pixel data")
    if is_compressed(ds):
        from pydicom.encaps import generate_frames
        for i, frame in enumerate(generate_frames(ds.PixelData, number_of_frames=n), start=1):
            if i == number:
                return frame
        raise IndexError("frame not found in encapsulated data")
    rows, cols = int(ds.Rows), int(ds.Columns)
    spp = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    bits = int(getattr(ds, "BitsAllocated", 16) or 16)
    if bits == 1:
        size = (rows * cols * spp + 7) // 8
    else:
        size = rows * cols * spp * (bits // 8)
    data = ds.PixelData
    start = (number - 1) * size
    return bytes(data[start:start + size])


def _first(v):
    if v is None:
        return None
    if hasattr(v, "__iter__") and not isinstance(v, (str, bytes)):
        v = list(v)
        return v[0] if v else None
    return v


def render(data: bytes, *, frame: int = 1, window_center: Optional[float] = None,
           window_width: Optional[float] = None, fmt: str = "png",
           max_size: int = 2048, quality: int = 90) -> bytes:
    """Display image of *frame* (1-based) with windowing applied."""
    from PIL import Image

    ds = load(data)
    if "PixelData" not in ds:
        raise ValueError("DICOM object has no pixel data")
    arr = ds.pixel_array
    n = frame_count(ds)
    if n > 1:
        arr = arr[max(0, min(frame - 1, arr.shape[0] - 1))]
    photometric = str(getattr(ds, "PhotometricInterpretation", "MONOCHROME2")).upper()

    if arr.ndim == 2:
        arr = arr.astype(np.float32)
        slope = float(getattr(ds, "RescaleSlope", 1) or 1)
        intercept = float(getattr(ds, "RescaleIntercept", 0) or 0)
        arr = arr * slope + intercept
        wc = window_center if window_center is not None else _first(getattr(ds, "WindowCenter", None))
        ww = window_width if window_width is not None else _first(getattr(ds, "WindowWidth", None))
        if wc is not None and ww is not None and float(ww) > 0:
            lo, hi = float(wc) - float(ww) / 2.0, float(wc) + float(ww) / 2.0
        else:
            lo, hi = float(arr.min()), float(arr.max())
        if hi <= lo:
            hi = lo + 1.0
        arr = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
        if photometric == "MONOCHROME1":
            arr = 1.0 - arr
        img = Image.fromarray((arr * 255.0).astype(np.uint8), mode="L")
    else:
        if photometric.startswith("YBR"):
            from pydicom.pixels import convert_color_space
            arr = convert_color_space(arr, photometric, "RGB")
        img = Image.fromarray(arr.astype(np.uint8))

    if max(img.size) > max_size:
        img.thumbnail((max_size, max_size))
    buf = io.BytesIO()
    if fmt in ("jpeg", "jpg"):
        img.convert("RGB" if img.mode not in ("L", "RGB") else img.mode).save(
            buf, format="JPEG", quality=quality)
    else:
        img.save(buf, format="PNG")
    return buf.getvalue()


def pixel_stats(data: bytes, frame: int = 1) -> dict:
    """Min/max/mean in modality units — handy for the agent and for tests."""
    ds = load(data)
    arr = ds.pixel_array
    if frame_count(ds) > 1:
        arr = arr[frame - 1]
    arr = arr.astype(np.float32) * float(getattr(ds, "RescaleSlope", 1) or 1) + \
        float(getattr(ds, "RescaleIntercept", 0) or 0)
    return {"min": float(arr.min()), "max": float(arr.max()), "mean": float(arr.mean())}
