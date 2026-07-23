"""Configuration loader — reads from environment / .env file.

Microservice URLs and secrets must come from env (see repo-root ``.env.example``).
No deploy hosts or absolute machine paths are baked into application logic.
"""
from __future__ import annotations
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
_PROJECT_ROOT = ROOT.parent
# Prefer project-root .env (shared by all services), then backend/.env.
load_dotenv(_PROJECT_ROOT / ".env")
load_dotenv(ROOT / ".env")
load_dotenv(_PROJECT_ROOT / ".env.example")  # documented local defaults only

ASR_MODEL_ID = os.getenv("ASR_MODEL_ID", "facebook/omniASR-LLM-7B")
ASR_DEVICE = os.getenv("ASR_DEVICE", "auto")
ASR_DTYPE = os.getenv("ASR_DTYPE", "bfloat16")
ASR_TARGET_SR = int(os.getenv("ASR_TARGET_SR", "16000"))

OLLAMA_HOST = (os.getenv("OLLAMA_HOST") or "").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL") or ""

# MedicalRAG microservice (HTTP). Alias MEDICALRAG_URL accepted by the client.
MEDRAG_API_URL = (
    os.getenv("MEDRAG_API_URL") or os.getenv("MEDICALRAG_URL") or ""
).rstrip("/")
MEDRAG_TIMEOUT_SEC = float(os.getenv("MEDRAG_TIMEOUT_SEC", "120"))

# Triton Inference Server (optional ASR path)
TRITON_URL = (os.getenv("TRITON_URL") or "").rstrip("/")
TRITON_MODEL = os.getenv("TRITON_MODEL") or "whisper"
TRITON_TIMEOUT_SEC = float(os.getenv("TRITON_TIMEOUT_SEC", "120"))
TRITON_PROTOCOL = os.getenv("TRITON_PROTOCOL") or "http"

TEMPLATES_DIR = (ROOT / os.getenv("TEMPLATES_DIR", "./data/templates")).resolve()
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
RADREPORT_BASE = os.getenv("RADREPORT_BASE", "https://radreport.org")
# Off by default — institutional templates must not be overwritten by radreport.org.
TEMPLATES_FETCH_RADREPORT = os.getenv("TEMPLATES_FETCH_RADREPORT", "0").strip().lower() in (
    "1", "true", "yes", "on",
)

# Hospital/insurance report-title rules (DOCX + optional plain-text sibling).
_REPORT_RULES_DIR = (ROOT / os.getenv("REPORT_RULES_DIR", "./data/report_rules")).resolve()
_REPORT_RULES_DIR.mkdir(parents=True, exist_ok=True)
_default_rules_docx = _REPORT_RULES_DIR / "hospital_insurance_report_titles.docx"
_default_rules_txt = _REPORT_RULES_DIR / "hospital_insurance_report_titles.txt"
REPORT_RULES_DIR = _REPORT_RULES_DIR
REPORT_RULES_DOCX = (
    Path(os.getenv("REPORT_RULES_DOCX", str(_default_rules_docx))).expanduser().resolve()
    if os.getenv("REPORT_RULES_DOCX")
    else _default_rules_docx.resolve()
)
REPORT_RULES_TXT = (
    Path(os.getenv("REPORT_RULES_TXT", str(_default_rules_txt))).expanduser().resolve()
    if os.getenv("REPORT_RULES_TXT")
    else _default_rules_txt.resolve()
)
REPORT_RULES_MAX_CHARS = int(os.getenv("REPORT_RULES_MAX_CHARS", "6000"))
# Ask MedicalRAG about naming rules during report generation (falls back to local excerpt).
REPORT_RULES_MEDRAG = os.getenv("REPORT_RULES_MEDRAG", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8010"))
