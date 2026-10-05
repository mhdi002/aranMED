"""Pixel access for WADO-RS frames / rendered / thumbnail.

* ``frame_bytes`` returns one frame exactly as stored: native pixels for
  uncompressed transfer syntaxes, the encapsulated fragment(s) for
  compressed ones. This is what WADO-RS ``/frames`` serves by default.
* ``native_frame`` decodes one frame of any transfer syntax to native
  little-endian pixels. ``/frames`` serves it when a client asks for Explicit
  VR Little Endian, i.e. standard DICOMweb transcoding.
* ``display_frame`` is what the in-app viewer loads. It returns pixels
  normalised to one of a few array types:
  - palette colour expanded to RGB;
  - YBR converted to RGB;
  - 1-bit unpacked;
  - Enhanced multi-frame per-frame rescale and window resolved.
  The viewer can then show every device's images with true modality values.
* ``render`` produces a display PNG/JPEG with the modality LUT (rescale) and
  VOI windowing applied, either the file's own window or one the caller
  passes.

Compressed objects are decoded with pydicom's pixel handlers: Pillow,
pylibjpeg (libjpeg/openjpeg), pyjpegls and GDCM. Together these cover JPEG
baseline, extended and lossless, JPEG-LS, JPEG 2000, HTJ2K and RLE. A failure
to decode is reported as :class:`RenderError`, never as a crash.
"""
from __future__ import annotations

import io
from typing import Any, Optional

import numpy as np

VIDEO_SYNTAXES = {
    "1.2.840.10008.1.2.4.100", "1.2.840.10008.1.2.4.100.1", "1.2.840.10008.1.2.4.101",
    "1.2.840.10008.1.2.4.101.1", "1.2.840.10008.1.2.4.102", "1.2.840.10008.1.2.4.102.1",
    "1.2.840.10008.1.2.4.103", "1.2.840.10008.1.2.4.103.1", "1.2.840.10008.1.2.4.104",
    "1.2.840.10008.1.2.4.104.1", "1.2.840.10008.1.2.4.105", "1.2.840.10008.1.2.4.105.1",
    "1.2.840.10008.1.2.4.106", "1.2.840.10008.1.2.4.106.1", "1.2.840.10008.1.2.4.107",
    "1.2.840.10008.1.2.4.108",
}


class RenderError(ValueError):
    """The object has no displayable pixels, or they could not be decoded."""


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


def is_video(ds) -> bool:
    return transfer_syntax(ds) in VIDEO_SYNTAXES


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
        data = ds.PixelData
        start_bit = (number - 1) * rows * cols * spp
        if start_bit % 8 == 0:
            return bytes(data[start_bit // 8:start_bit // 8 + size])
        from pydicom.pixels import pack_bits, unpack_bits
        bits_all = unpack_bits(data)[start_bit:start_bit + rows * cols * spp]
        return pack_bits(bits_all)
    size = rows * cols * spp * (bits // 8)
    data = ds.PixelData
    start = (number - 1) * size
    return bytes(data[start:start + size])


# ----------------------------------------------------------------- decoding
def decode_frame(ds, frame: int = 1) -> np.ndarray:
    """Stored values of one frame (1-based), decoded from any transfer syntax.
    Colour is returned as RGB (YBR converted once, by pydicom)."""
    if "PixelData" not in ds and "FloatPixelData" not in ds and "DoubleFloatPixelData" not in ds:
        raise RenderError("DICOM object has no pixel data")
    if is_video(ds):
        raise RenderError("video object: use the /video endpoint")
    n = frame_count(ds)
    if frame < 1 or frame > n:
        raise IndexError(f"frame {frame} out of range 1..{n}")
    from pydicom.pixels import pixel_array
    try:
        arr = pixel_array(ds, index=frame - 1 if n > 1 else None)
    except Exception as e:  # noqa: BLE001  (each decoder raises its own type)
        raise RenderError(f"cannot decode {transfer_syntax(ds)}: {e}") from e
    if n > 1 and arr.ndim >= 3 and arr.shape[0] == 1 and int(getattr(ds, "SamplesPerPixel", 1) or 1) == 1:
        arr = arr[0]
    return arr


def _seq_attr(ds, frame: int, seq: str, attr: str):
    """An attribute from an Enhanced multi-frame functional group (per-frame
    first, then shared), falling back to the top-level attribute."""
    for groups, idx in ((getattr(ds, "PerFrameFunctionalGroupsSequence", None), frame - 1),
                        (getattr(ds, "SharedFunctionalGroupsSequence", None), 0)):
        if groups is not None and len(groups) > idx:
            items = getattr(groups[idx], seq, None)
            if items and attr in items[0]:
                return getattr(items[0], attr)
    return getattr(ds, attr, None)


def _first(v):
    if v is None:
        return None
    if hasattr(v, "__iter__") and not isinstance(v, (str, bytes)):
        v = list(v)
        return v[0] if v else None
    return v


def frame_params(ds, frame: int = 1) -> dict[str, Any]:
    """Rescale and window for *frame*, honouring Enhanced functional groups
    and RT Dose grid scaling."""
    slope = _seq_attr(ds, frame, "PixelValueTransformationSequence", "RescaleSlope")
    intercept = _seq_attr(ds, frame, "PixelValueTransformationSequence", "RescaleIntercept")
    if slope is None and getattr(ds, "DoseGridScaling", None) is not None:
        slope = ds.DoseGridScaling
    wc = _first(_seq_attr(ds, frame, "FrameVOILUTSequence", "WindowCenter"))
    ww = _first(_seq_attr(ds, frame, "FrameVOILUTSequence", "WindowWidth"))
    return {"slope": float(slope) if slope not in (None, "") else 1.0,
            "intercept": float(intercept) if intercept not in (None, "") else 0.0,
            "wc": float(wc) if wc not in (None, "") else None,
            "ww": float(ww) if ww not in (None, "") else None,
            "photometric": str(getattr(ds, "PhotometricInterpretation", "MONOCHROME2")).upper()}


def _palette_to_rgb(arr: np.ndarray, ds) -> np.ndarray:
    from pydicom.pixels import apply_color_lut
    rgb = apply_color_lut(arr, ds)
    try:
        bits = int(ds.RedPaletteColorLookupTableDescriptor[2])
    except (AttributeError, IndexError, TypeError, ValueError):
        bits = 16 if rgb.max() > 255 else 8
    if bits > 8:
        rgb = (rgb.astype(np.uint32) >> (bits - 8)).astype(np.uint8)
    return rgb.astype(np.uint8)


def _to_u8_rgb(arr: np.ndarray, bits_stored: Optional[int] = None) -> np.ndarray:
    """8-bit RGB for display from 8/16/32-bit colour samples."""
    if arr.dtype == np.uint8:
        return arr
    bits = int(bits_stored or arr.dtype.itemsize * 8)
    if bits <= 8:
        return arr.astype(np.uint8)
    return (arr.astype(np.uint64) >> (bits - 8)).clip(0, 255).astype(np.uint8)


def native_frame(ds, frame: int = 1) -> bytes:
    """One frame decoded to native little-endian pixels (DICOMweb transcoding)."""
    arr = decode_frame(ds, frame)
    if arr.dtype.byteorder == ">" or (arr.dtype.byteorder == "=" and np.little_endian is False):
        arr = arr.byteswap().view(arr.dtype.newbyteorder("<"))
    return np.ascontiguousarray(arr).tobytes()


_FORMATS = {np.dtype("uint8"): "u8", np.dtype("int8"): "i8", np.dtype("uint16"): "u16",
            np.dtype("int16"): "i16", np.dtype("uint32"): "u32", np.dtype("int32"): "i32",
            np.dtype("float32"): "f32", np.dtype("float64"): "f32"}


def display_frame(ds, frame: int = 1) -> tuple[bytes, dict[str, Any]]:
    """Viewer-ready pixels plus how to read them: (bytes, info) where info has
    ``format`` (u8/i8/u16/i16/u32/i32/f32), ``samples`` (1 or 3),
    ``photometric`` (MONOCHROME1/MONOCHROME2/RGB), ``rows``/``columns``,
    per-frame ``slope``/``intercept`` and ``wc``/``ww``."""
    arr = decode_frame(ds, frame)
    p = frame_params(ds, frame)
    photometric = p["photometric"]
    if photometric == "PALETTE COLOR":
        arr, photometric = _palette_to_rgb(arr, ds), "RGB"
    elif arr.ndim == 3:
        arr, photometric = _to_u8_rgb(arr, getattr(ds, "BitsStored", None)), "RGB"
    if arr.dtype == bool:
        arr = arr.astype(np.uint8)
    if arr.dtype == np.float64:
        arr = arr.astype(np.float32)
    if arr.dtype not in _FORMATS:
        arr = arr.astype(np.float32)
    arr = np.ascontiguousarray(arr.astype(arr.dtype.newbyteorder("<")))
    color = arr.ndim == 3
    info = {"format": _FORMATS[np.dtype(arr.dtype.name)], "samples": 3 if color else 1,
            "photometric": "RGB" if color else (photometric if photometric.startswith("MONOCHROME")
                                                else "MONOCHROME2"),
            "rows": int(arr.shape[0]), "columns": int(arr.shape[1]),
            "slope": 1.0 if color else p["slope"], "intercept": 0.0 if color else p["intercept"],
            "wc": None if color else p["wc"], "ww": None if color else p["ww"]}
    return arr.tobytes(), info


def render(data: bytes, *, frame: int = 1, window_center: Optional[float] = None,
           window_width: Optional[float] = None, fmt: str = "png",
           max_size: int = 2048, quality: int = 90) -> bytes:
    """Display image of *frame* (1-based) with windowing applied."""
    from PIL import Image

    ds = load(data)
    arr = decode_frame(ds, frame)
    p = frame_params(ds, frame)
    photometric = p["photometric"]
    if photometric == "PALETTE COLOR":
        arr = _palette_to_rgb(arr, ds)

    if arr.ndim == 2:
        arr = arr.astype(np.float32) * p["slope"] + p["intercept"]
        wc = window_center if window_center is not None else p["wc"]
        ww = window_width if window_width is not None else p["ww"]
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
        img = Image.fromarray(_to_u8_rgb(arr, getattr(ds, "BitsStored", None)), mode="RGB")

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
    arr = decode_frame(ds, frame)
    p = frame_params(ds, frame)
    arr = arr.astype(np.float32) * p["slope"] + p["intercept"]
    return {"min": float(arr.min()), "max": float(arr.max()), "mean": float(arr.mean())}


# ----------------------------------------------------------------- non-image objects
def sr_text(ds) -> dict[str, Any]:
    """A Structured Report as readable text plus its content tree."""
    def node(item, depth: int) -> tuple[dict, list[str]]:
        name = ""
        cn = getattr(item, "ConceptNameCodeSequence", None)
        if cn:
            name = str(getattr(cn[0], "CodeMeaning", "") or "")
        vtype = str(getattr(item, "ValueType", "") or "")
        value: Any = None
        if vtype == "TEXT":
            value = str(getattr(item, "TextValue", ""))
        elif vtype == "NUM":
            mv = getattr(item, "MeasuredValueSequence", None)
            if mv:
                unit = getattr(getattr(mv[0], "MeasurementUnitsCodeSequence", [None])[0], "CodeValue", "") \
                    if getattr(mv[0], "MeasurementUnitsCodeSequence", None) else ""
                value = f"{mv[0].NumericValue} {unit}".strip()
        elif vtype == "CODE":
            cc = getattr(item, "ConceptCodeSequence", None)
            value = str(getattr(cc[0], "CodeMeaning", "")) if cc else None
        elif vtype in ("DATE", "TIME", "DATETIME", "UIDREF", "PNAME"):
            value = str(getattr(item, {"DATE": "Date", "TIME": "Time", "DATETIME": "DateTime",
                                       "UIDREF": "UID", "PNAME": "PersonName"}[vtype], ""))
        lines = [("  " * depth) + (f"{name}: {value}" if value not in (None, "") else name)] if (name or value) else []
        kids = []
        for child in getattr(item, "ContentSequence", None) or []:
            c, cl = node(child, depth + 1)
            kids.append(c)
            lines += cl
        return {"name": name, "type": vtype, "value": value, "children": kids}, lines
    tree, lines = node(ds, 0)
    return {"title": tree["name"], "text": "\n".join(x for x in lines if x.strip()), "tree": tree,
            "completion": str(getattr(ds, "CompletionFlag", "") or ""),
            "verification": str(getattr(ds, "VerificationFlag", "") or "")}


def encapsulated_document(ds) -> tuple[bytes, str]:
    """(bytes, MIME type) of an Encapsulated PDF/CDA/STL/OBJ object."""
    data = getattr(ds, "EncapsulatedDocument", None)
    if data is None:
        raise RenderError("not an encapsulated document")
    mime = str(getattr(ds, "MIMETypeOfEncapsulatedDocument", "") or "application/octet-stream")
    raw = bytes(data)
    length = getattr(ds, "EncapsulatedDocumentLength", None)
    if length:
        raw = raw[:int(length)]
    elif mime == "application/pdf" and raw.endswith(b"\x00"):
        raw = raw.rstrip(b"\x00")
    return raw, mime


VIDEO_MIME = {
    "1.2.840.10008.1.2.4.100": "video/mpeg", "1.2.840.10008.1.2.4.100.1": "video/mpeg",
    "1.2.840.10008.1.2.4.101": "video/mpeg", "1.2.840.10008.1.2.4.101.1": "video/mpeg",
}


def video_stream(ds) -> tuple[bytes, str]:
    """The encoded video bitstream (MPEG-2 / H.264 / HEVC) and its MIME type."""
    if not is_video(ds):
        raise RenderError("not a video object")
    from pydicom.encaps import generate_fragments
    data = b"".join(generate_fragments(ds.PixelData))
    ts = transfer_syntax(ds)
    mime = VIDEO_MIME.get(ts) or ("video/mp4" if ts.startswith(("1.2.840.10008.1.2.4.102",
                                                                  "1.2.840.10008.1.2.4.103",
                                                                  "1.2.840.10008.1.2.4.104",
                                                                  "1.2.840.10008.1.2.4.105",
                                                                  "1.2.840.10008.1.2.4.106",
                                                                  "1.2.840.10008.1.2.4.107",
                                                                  "1.2.840.10008.1.2.4.108"))
                                  else "application/octet-stream")
    return data, mime
