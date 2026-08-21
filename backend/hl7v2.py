"""HL7 v2.x message parsing and generation.

HL7 v2 is how most hospital systems still talk: ADT for admissions and
demographics, ORM for orders, ORU for results. This handles the subset that
matters for a reporting system — reading patient/order context in, and
emitting a finished report out — without taking a dependency, because v2's
pipe-and-hat encoding is genuinely simple and the parsers available are
heavier than the format.

Supported:

* Parse any v2 message into segments/fields, with the encoding characters
  taken from MSH-2 rather than assumed.
* Extract patient demographics (PID) and order/observation context
  (PV1/ORC/OBR) into the internal EHR shape.
* Build an ORU^R01 result message carrying a finished report, which is how a
  report gets back to the ordering system.

Not supported: MLLP networking (a listener belongs in its own service, not in
the API process), v3/CDA, and the full segment catalogue. Unknown segments are
preserved verbatim rather than dropped, so nothing is silently lost.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("hl7v2")

_DEFAULT_ENCODING = {"field": "|", "component": "^", "repeat": "~",
                     "escape": "\\", "subcomponent": "&"}


@dataclass
class Hl7Message:
    segments: list[list[str]] = field(default_factory=list)
    encoding: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_ENCODING))

    def segment(self, name: str) -> Optional[list[str]]:
        for seg in self.segments:
            if seg and seg[0] == name:
                return seg
        return None

    def all_segments(self, name: str) -> list[list[str]]:
        return [s for s in self.segments if s and s[0] == name]

    def field(self, segment: str, index: int, default: str = "") -> str:
        """Field by HL7 numbering (MSH-9 is index 9), 1-based as the spec is."""
        seg = self.segment(segment)
        if not seg:
            return default
        # MSH is offset by one: MSH-1 *is* the field separator, so MSH-2 lands
        # at list position 1. Every other segment numbers from its name.
        idx = index - 1 if segment == "MSH" else index
        if 0 <= idx < len(seg):
            return seg[idx]
        return default

    def component(self, segment: str, index: int, comp: int, default: str = "") -> str:
        raw = self.field(segment, index)
        if not raw:
            return default
        parts = raw.split(self.encoding["component"])
        return parts[comp - 1] if 0 < comp <= len(parts) else default

    @property
    def message_type(self) -> str:
        return self.field("MSH", 9)

    @property
    def control_id(self) -> str:
        return self.field("MSH", 10)


def parse(text: str | bytes) -> Hl7Message:
    """Parse a v2 message. Accepts \\r, \\n or \\r\\n segment terminators."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = text.strip().lstrip("\x0b").rstrip("\x1c\r\n")
    if not text.startswith("MSH"):
        raise ValueError("not an HL7 v2 message (no MSH segment)")

    encoding = dict(_DEFAULT_ENCODING)
    encoding["field"] = text[3]
    enc_chars = text[4:8]
    if len(enc_chars) >= 4:
        encoding["component"] = enc_chars[0]
        encoding["repeat"] = enc_chars[1]
        encoding["escape"] = enc_chars[2]
        encoding["subcomponent"] = enc_chars[3]

    segments: list[list[str]] = []
    for line in re.split(r"[\r\n]+", text):
        line = line.strip()
        if line:
            segments.append(line.split(encoding["field"]))
    return Hl7Message(segments=segments, encoding=encoding)


def to_ehr(msg: Hl7Message) -> dict[str, Any]:
    """Patient and encounter context from PID/PV1/OBR, in the internal shape."""
    out: dict[str, Any] = {"patient": {}, "encounter": {}}

    # PID-3 identifier list, PID-5 name, PID-7 DOB, PID-8 sex.
    mrn = msg.component("PID", 3, 1)
    if mrn:
        out["patient"]["mrn"] = mrn
    family = msg.component("PID", 5, 1)
    given = msg.component("PID", 5, 2)
    name = " ".join(p for p in (given, family) if p)
    if name:
        out["patient"]["name"] = name
    sex = msg.field("PID", 8).upper()
    if sex in ("M", "F", "O"):
        out["patient"]["sex"] = {"M": "male", "F": "female", "O": "other"}[sex]
    dob = msg.field("PID", 7)
    if dob and len(dob) >= 4:
        try:
            out["patient"]["age"] = float(time.gmtime().tm_year - int(dob[:4]))
        except ValueError:
            pass

    # OBR-4 is the study/service; PV1-2 the patient class.
    study = msg.component("OBR", 4, 2) or msg.component("OBR", 4, 1)
    if study:
        out["encounter"]["chief_complaint"] = study
    accession = msg.field("OBR", 3) or msg.field("ORC", 3)
    if accession:
        out["encounter"]["accession_number"] = accession.split(msg.encoding["component"])[0]
    when = msg.field("OBR", 7) or msg.field("MSH", 7)
    if when:
        out["encounter"]["date"] = when

    out["patient"] = {k: v for k, v in out["patient"].items() if v}
    out["encounter"] = {k: v for k, v in out["encounter"].items() if v}
    return out


def _now_ts() -> str:
    return time.strftime("%Y%m%d%H%M%S", time.gmtime())


def _escape(value: str, encoding: dict[str, str]) -> str:
    """Escape separators so report text can never break the framing."""
    esc = encoding["escape"]
    out = value.replace(esc, f"{esc}E{esc}")
    out = out.replace(encoding["field"], f"{esc}F{esc}")
    out = out.replace(encoding["component"], f"{esc}S{esc}")
    out = out.replace(encoding["repeat"], f"{esc}R{esc}")
    out = out.replace(encoding["subcomponent"], f"{esc}T{esc}")
    return out


def build_oru(*, patient_id: str, patient_name: str = "",
              report_text: str, accession: str = "",
              study_description: str = "",
              sending_app: str = "ARANMED",
              sending_facility: str = "",
              receiving_app: str = "", receiving_facility: str = "",
              control_id: Optional[str] = None,
              observation_status: str = "F") -> str:
    """An ORU^R01 carrying a finished report.

    Report lines become repeated OBX-5 segments, which is how narrative
    radiology results travel in v2. ``observation_status`` is ``F`` (final)
    or ``P`` (preliminary) — a draft must not be sent as final.
    """
    enc = _DEFAULT_ENCODING
    ts = _now_ts()
    cid = control_id or f"ARM{ts}"
    seg_sep = "\r"

    msh = ["MSH", "^~\\&", sending_app, sending_facility,
           receiving_app, receiving_facility, ts, "", "ORU^R01", cid, "P", "2.5"]
    pid = ["PID", "1", "", patient_id, "", patient_name, "", "", ""]
    obr = ["OBR", "1", accession, accession, study_description,
           "", "", ts, "", "", "", "", "", "", "", "", "", "", "", "", "",
           "", "", "", "", observation_status]

    segments = [msh, pid, obr]
    for i, line in enumerate((report_text or "").splitlines() or [""], start=1):
        segments.append(["OBX", str(i), "TX", "&GDT^Report Text", "1",
                         _escape(line, enc), "", "", "", "", "", observation_status])

    return seg_sep.join(enc["field"].join(s) for s in segments)


def build_ack(msg: Hl7Message, *, code: str = "AA",
              text: str = "", sending_app: str = "ARANMED") -> str:
    """An ACK for a received message. ``AA`` accept, ``AE`` error, ``AR`` reject."""
    ts = _now_ts()
    msh = ["MSH", "^~\\&", sending_app, msg.field("MSH", 6),
           msg.field("MSH", 3), msg.field("MSH", 4), ts, "", "ACK",
           f"ACK{ts}", "P", msg.field("MSH", 12) or "2.5"]
    msa = ["MSA", code, msg.control_id or "", text]
    return "\r".join("|".join(s) for s in (msh, msa))
