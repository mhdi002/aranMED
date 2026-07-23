"""Unified configuration loader for MedicalRAG.

Paths resolve relative to project ROOT unless absolute.
Environment overrides (deploy-friendly):

  MEDRAG_ROOT           Project root (default: repo containing this package)
  MEDRAG_DATA_DIR       Override data_dir (catalog siblings, OCR, archives, …)
  MEDRAG_CATALOG_DB     Override catalog.db path
  MEDRAG_LIBRARY_DIR    EN textbook library
  MEDRAG_EXAM_DIR       Exam books directory
  MEDRAG_MEHRSYS_DIR    Mehrsys packs root
  MEDRAG_STANDARDS_DIR  Iranian standards zip/rar root
  MEDRAG_QDRANT_STORAGE Live Qdrant server storage directory
  QDRANT_URL            Qdrant HTTP URL (default from config.yaml)
  MEDRAG_QDRANT_MODE    "server" | "local"
  MEDRAG_CONFIG         Alternate config.yaml path
  MEDRAG_LLM_PROVIDER   vllm | ollama | openai  (alias: llm.provider)
  MEDRAG_LLM_BASE_URL   OpenAI-compatible base (alias: VLLM_BASE_URL)
  MEDRAG_LLM_MODEL      Served model name
  MEDRAG_LLM_API_KEY    Optional bearer token (vLLM often unused)
  MEDRAG_LLM_FALLBACK   Optional fallback provider (e.g. ollama)
  MEDRAG_LLM_TEMPERATURE / MEDRAG_LLM_MAX_TOKENS / MEDRAG_LLM_TOP_P
  MEDRAG_LLM_ENABLE_THINKING / MEDRAG_LLM_STRIP_THINKING / MEDRAG_LLM_NUM_CTX
  MEDRAG_EMBED_PROVIDER local | vllm | openai  (alias: embeddings.provider)
  MEDRAG_EMBED_BASE_URL OpenAI-compatible embeddings base (alias: VLLM_EMBED_BASE_URL)
  MEDRAG_EMBED_MODEL    Embedding model id (alias: embeddings.model)
  MEDRAG_EMBED_API_KEY  Optional bearer for embeddings endpoint
  MEDRAG_MIN_CE_SCORE / MEDRAG_GEN_CONTEXT / MEDRAG_GEN_CHARS
  MEDRAG_GROUNDING_MIN / MEDRAG_SELF_RAG_THRESHOLD
  MEDRAG_UNLOAD_BEFORE_GENERATE  Release local CUDA embeds before LLM gen
  MEDRAG_API_HOST / MEDRAG_API_PORT  FastAPI bind (interfaces.api)
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

_PKG_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency). Does not override existing env vars."""
    if not path.is_file():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            key, _, val = s.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()


# Load .env from package-discovered root first, then MEDRAG_ROOT if different
_load_dotenv(_PKG_ROOT / ".env")
_root_early = Path(_env("MEDRAG_ROOT", str(_PKG_ROOT))).expanduser().resolve()
if _root_early != _PKG_ROOT.resolve():
    _load_dotenv(_root_early / ".env")

ROOT = Path(_env("MEDRAG_ROOT", str(_PKG_ROOT))).expanduser().resolve()
_CFG_PATH = Path(_env("MEDRAG_CONFIG", str(ROOT / "config.yaml"))).expanduser()
if not _CFG_PATH.is_absolute():
    _CFG_PATH = (ROOT / _CFG_PATH).resolve()


def _load() -> dict:
    with open(_CFG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


CFG = _load()
PATHS = CFG.get("paths") or {}


def _resolve(raw: str | Path | None, *, base: Path | None = None) -> Path | None:
    """Resolve a path string: absolute stays absolute; relative joins base (or ROOT)."""
    if raw is None or str(raw).strip() == "":
        return None
    p = Path(str(raw)).expanduser()
    if p.is_absolute():
        return p
    return ((base or ROOT) / p).resolve()


def path(key: str, default: str | None = None) -> Path:
    """Lookup paths.<key> from config.yaml (or default) and resolve under ROOT."""
    raw = PATHS.get(key, default)
    if raw is None or str(raw).strip() == "":
        raise KeyError(f"paths.{key} is not set in {_CFG_PATH}")
    resolved = _resolve(raw, base=ROOT)
    assert resolved is not None
    return resolved


def optional_path(key: str, default: str | None = None) -> Path | None:
    raw = PATHS.get(key, default)
    return _resolve(raw, base=ROOT)


# --- Data root (optional override relocates relative data_* under this dir) ---
_data_override = _env("MEDRAG_DATA_DIR")
if _data_override:
    DATA_DIR = Path(_data_override).expanduser().resolve()
else:
    DATA_DIR = path("data_dir", "data")


def data_path(key: str, default: str | None = None) -> Path:
    """Resolve a paths key; if relative and MEDRAG_DATA_DIR is set, prefer under DATA_DIR.

    Absolute config values always win. Relative values that already start with
    the configured data_dir name stay under ROOT/data_dir unless MEDRAG_DATA_DIR
    is set (then they are remapped under that override).
    """
    raw = PATHS.get(key, default)
    if raw is None or str(raw).strip() == "":
        raise KeyError(f"paths.{key} is not set")
    p = Path(str(raw)).expanduser()
    if p.is_absolute():
        return p
    if _data_override:
        # Strip a leading "data/" or configured data_dir prefix so override is clean
        parts = p.parts
        cfg_data = Path(PATHS.get("data_dir", "data"))
        if parts and parts[0] == cfg_data.parts[0]:
            p = Path(*parts[1:]) if len(parts) > 1 else Path(".")
        return (DATA_DIR / p).resolve() if str(p) != "." else DATA_DIR
    return (ROOT / p).resolve()


# Convenience exports (medrag-compatible names)
EXAM_BOOKS_DIR = (
    _resolve(_env("MEDRAG_EXAM_DIR"))
    or path("exam_books_dir", "..")
)
LIBRARY_DIR = (
    _resolve(_env("MEDRAG_LIBRARY_DIR"))
    or path("library_dir", "../../Data-main/medrag/library")
)
_mehrsys_env = _env("MEDRAG_MEHRSYS_DIR")
_mehrsys_cfg = PATHS.get("mehrsys_books_dir") or ""
MEHRSYS_BOOKS_DIR = (
    _resolve(_mehrsys_env)
    or (_resolve(_mehrsys_cfg) if str(_mehrsys_cfg).strip() else None)
)
_standards_env = _env("MEDRAG_STANDARDS_DIR")
_standards_cfg = PATHS.get("standards_dir") or ""
STANDARDS_DIR = (
    _resolve(_standards_env)
    or (_resolve(_standards_cfg) if str(_standards_cfg).strip() else None)
)

_catalog_env = _env("MEDRAG_CATALOG_DB")
CATALOG_DB = (
    _resolve(_catalog_env)
    or path("catalog_db", "catalog.db")
)

MANIFEST = data_path("manifest", "data/manifest.jsonl")
TEXT_OUT = data_path("text_out", "data/text")
OCR_OUT = data_path("ocr_out", "data/ocr_markdown")
CHUNKS = data_path("chunks", "data/chunks.jsonl")
REPORTS_DIR = path("reports_dir", "reports")
EVAL_DIR = path("eval_dir", "eval")
STANDARDS_EXTRACTED = data_path("standards_extracted", "data/standards_extracted")

# Live server storage (native binary / Docker volume). Do not confuse with local archives.
QDRANT_SERVER_STORAGE = (
    _resolve(_env("MEDRAG_QDRANT_STORAGE"))
    or path("qdrant_storage", "qdrant_storage")
)

# Local-mode / legacy archive stores (server mode does not open these)
QDRANT_ARCHIVES_DIR = (
    optional_path("qdrant_archives_dir", "data/qdrant_archives")
    or (DATA_DIR / "qdrant_archives")
)


def _local_store(key: str, folder_name: str) -> Path:
    """Resolve a legacy local Qdrant store under archives (with root-level fallback).

    Target layout: data/qdrant_archives/<name>. Until relocate runs, folders may
    still sit at repo root — prefer an existing path so locks/scripts keep working.
    """
    preferred = data_path(key) if PATHS.get(key) else (QDRANT_ARCHIVES_DIR / folder_name).resolve()
    if preferred.exists():
        return preferred
    legacy = (ROOT / folder_name).resolve()
    if legacy.exists():
        return legacy
    return preferred


# Main local store: vector_db.storage_path if absolute, else paths.qdrant_data / archives
_vd_storage = (CFG.get("vector_db") or {}).get("storage_path")
if _vd_storage and Path(str(_vd_storage)).is_absolute():
    QDRANT_STORAGE = Path(str(_vd_storage)).expanduser().resolve()
else:
    QDRANT_STORAGE = _local_store("qdrant_data", "qdrant_data")

QDRANT_STANDARDS_STORAGE = _local_store("qdrant_standards_data", "qdrant_standards_data")
QDRANT_EXPAND_STORAGE = _local_store("qdrant_expand_data", "qdrant_expand_data")
QDRANT_EXPAND2_STORAGE = _local_store("qdrant_expand2_data", "qdrant_expand2_data")
QDRANT_EXPAND3_STORAGE = _local_store("qdrant_expand3_data", "qdrant_expand3_data")

_vd = CFG.get("vector_db") or {}
QDRANT_URL = _env("QDRANT_URL") or _vd.get("url") or ""
if not QDRANT_URL:
    raise ValueError("vector_db.url or QDRANT_URL must be set in config.yaml / env")
QDRANT_MODE = (_env("MEDRAG_QDRANT_MODE") or _vd.get("mode") or "server").lower()
COLLECTION = _vd.get("collection") or "medical_library"
DENSE_DIM = int(_vd.get("dense_dim") or 1024)

# Logical write store → server collection medical_library_{store} (prefer "expand")
_raw_write = PATHS.get("embed_write_store", "expand")
EMBED_WRITE_STORE = _raw_write if _raw_write in ("expand", "expand2", "expand3") else "expand"

# --- LLM generator (vLLM primary / Ollama fallback / OpenAI-compatible) ---
# Prefer optional top-level `llm:` block; else `generator:` (legacy name).
# Defaults live in config.yaml — env overrides only; no model/URL literals here.
_llm = {**(CFG.get("generator") or {}), **(CFG.get("llm") or {})}
_api = CFG.get("api") or {}

LLM_PROVIDER = (
    _env("MEDRAG_LLM_PROVIDER")
    or _llm.get("provider")
    or ""
).lower().strip()
if not LLM_PROVIDER:
    raise ValueError("llm.provider or MEDRAG_LLM_PROVIDER must be set")

# OpenAI-compatible base URL for vLLM / OpenAI / LM Studio.
# Env wins: MEDRAG_LLM_BASE_URL → VLLM_BASE_URL → config endpoint/base_url.
_llm_endpoint = _llm.get("base_url") or _llm.get("endpoint") or ""
LLM_BASE_URL = (
    _env("MEDRAG_LLM_BASE_URL")
    or _env("VLLM_BASE_URL")
    or str(_llm_endpoint)
).rstrip("/")
if not LLM_BASE_URL:
    raise ValueError(
        "llm.base_url / MEDRAG_LLM_BASE_URL / VLLM_BASE_URL must be set"
    )

LLM_MODEL = _env("MEDRAG_LLM_MODEL") or _llm.get("model") or ""
if not LLM_MODEL:
    raise ValueError("llm.model or MEDRAG_LLM_MODEL must be set")
LLM_API_KEY = _env("MEDRAG_LLM_API_KEY") or _env("OPENAI_API_KEY") or _llm.get("api_key") or ""

# Ollama native endpoint (OCR/VLM and optional chat fallback).
# Never reuse a .../v1 OpenAI-compatible URL as the Ollama host.


def _is_openai_compat_url(url: str | None) -> bool:
    if not url:
        return False
    u = str(url).rstrip("/").lower()
    return u.endswith("/v1") or "/v1/" in u or u.endswith("/chat/completions")


_ollama_from_cfg = None
if LLM_PROVIDER == "ollama":
    for cand in (_llm.get("endpoint"), _llm.get("base_url")):
        if cand and not _is_openai_compat_url(str(cand)):
            _ollama_from_cfg = str(cand)
            break

_vlm_endpoint = (CFG.get("vlm") or {}).get("endpoint") or ""
OLLAMA_URL = (
    _env("MEDRAG_OLLAMA_URL")
    or _env("OLLAMA_HOST")
    or _ollama_from_cfg
    or _llm.get("fallback_endpoint")
    or _vlm_endpoint
    or ""
)
if isinstance(OLLAMA_URL, str):
    OLLAMA_URL = OLLAMA_URL.rstrip("/")
# If someone pointed OLLAMA_HOST at a /v1 URL by mistake, fall back
if _is_openai_compat_url(OLLAMA_URL):
    OLLAMA_URL = str(_llm.get("fallback_endpoint") or _vlm_endpoint or "").rstrip("/")

LLM_FALLBACK_PROVIDER = (
    _env("MEDRAG_LLM_FALLBACK")
    or _llm.get("fallback_provider")
    or ""
).lower().strip() or None
LLM_FALLBACK_MODEL = (
    _env("MEDRAG_LLM_FALLBACK_MODEL")
    or _llm.get("fallback_model")
    or None
)
LLM_FALLBACK_ENDPOINT = (
    _env("MEDRAG_LLM_FALLBACK_ENDPOINT")
    or _llm.get("fallback_endpoint")
    or OLLAMA_URL
)
LLM_TIMEOUT = float(_env("MEDRAG_LLM_TIMEOUT") or _llm.get("timeout") or 300)
LLM_MAX_RETRIES = int(_env("MEDRAG_LLM_MAX_RETRIES") or _llm.get("max_retries") or 1)
LLM_TEMPERATURE = float(_env("MEDRAG_LLM_TEMPERATURE") or _llm.get("temperature") or 0.1)
LLM_MAX_TOKENS = int(_env("MEDRAG_LLM_MAX_TOKENS") or _llm.get("max_tokens") or 768)
LLM_TOP_P = float(_env("MEDRAG_LLM_TOP_P") or _llm.get("top_p") or 0.8)
LLM_NUM_CTX = int(_env("MEDRAG_LLM_NUM_CTX") or _llm.get("num_ctx") or 4096)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


_cfg_think = _llm.get("enable_thinking")
if _cfg_think is None:
    _cfg_think = False
LLM_ENABLE_THINKING = _env_bool("MEDRAG_LLM_ENABLE_THINKING", bool(_cfg_think))
_cfg_strip = _llm.get("strip_thinking")
if _cfg_strip is None:
    _cfg_strip = True
LLM_STRIP_THINKING = _env_bool("MEDRAG_LLM_STRIP_THINKING", bool(_cfg_strip))

API_HOST = _env("MEDRAG_API_HOST") or _api.get("host") or "0.0.0.0"
API_PORT = int(_env("MEDRAG_API_PORT") or _api.get("port") or 8080)

VISION_MODEL = CFG["vlm"]["model"]
OCR_MODEL = CFG["ocr"]["model"]
OCR_PROMPT = CFG["ocr"]["prompt"]
OCR_DPI = CFG["ocr"]["render_dpi"]
OCR_JPEG_QUALITY = CFG["ocr"].get("jpeg_quality", 95)
OCR_KEEP_ALIVE = CFG["ocr"].get("keep_alive", -1)
OCR_NUM_CTX = CFG["ocr"].get("num_ctx", 6144)
OCR_NUM_PREDICT = CFG["ocr"].get("num_predict", 3500)
OCR_PREFETCH = CFG["ocr"].get("prefetch_pages", True)
OCR_SKIP_BLANK = CFG["ocr"].get("skip_blank_pages", True)

# --- Embeddings (local FlagEmbedding | vLLM / OpenAI-compatible /v1/embeddings) ---
_emb = CFG.get("embeddings") or {}
EMBED_PROVIDER = (
    _env("MEDRAG_EMBED_PROVIDER")
    or _emb.get("provider")
    or "local"
).lower().strip()
EMBED_MODEL = (
    _env("MEDRAG_EMBED_MODEL")
    or _emb.get("model")
    or ""
)
if not EMBED_MODEL:
    raise ValueError("embeddings.model or MEDRAG_EMBED_MODEL must be set")
_embed_endpoint = _emb.get("base_url") or _emb.get("endpoint") or ""
EMBED_BASE_URL = (
    _env("MEDRAG_EMBED_BASE_URL")
    or _env("VLLM_EMBED_BASE_URL")
    or str(_embed_endpoint)
).rstrip("/")
EMBED_API_KEY = (
    _env("MEDRAG_EMBED_API_KEY")
    or _emb.get("api_key")
    or ""
)
EMBED_TIMEOUT = float(_env("MEDRAG_EMBED_TIMEOUT") or _emb.get("timeout") or 120)
EMBED_MAX_LENGTH = int(_emb.get("max_length") or 512)
SPARSE_EMBED = bool(_emb.get("sparse_enabled", True))
# OpenAI-compat dense APIs cannot return bge-m3 sparse/ColBERT — force off for vllm/openai
if EMBED_PROVIDER in ("vllm", "openai"):
    SPARSE_EMBED = False
    if not EMBED_BASE_URL:
        raise ValueError(
            "embeddings.base_url / MEDRAG_EMBED_BASE_URL / VLLM_EMBED_BASE_URL "
            "required when embeddings.provider is vllm|openai"
        )
RERANK_MODEL = (
    _env("MEDRAG_RERANK_MODEL")
    or (CFG.get("reranker") or {}).get("model")
    or ""
)
if not RERANK_MODEL:
    raise ValueError("reranker.model or MEDRAG_RERANK_MODEL must be set")
RERANK_DEVICE = (
    _env("MEDRAG_RERANK_DEVICE")
    or (CFG.get("reranker") or {}).get("device")
    or ""
)
EMBED_BATCH = int(
    _env("MEDRAG_EMBED_BATCH")
    or _emb.get("batch_size")
    or 32
)

CHUNK_TOKENS = CFG["chunking"]["textbook_tokens"]
CHUNK_OVERLAP = CFG["chunking"]["textbook_overlap"]
MIN_CHUNK_CHARS = CFG["chunking"]["min_chunk_chars"]

RETRIEVAL = dict(CFG.get("retrieval") or {})
# Env overrides for deploy / 8GB tuning (do not hardcode machine values in code)
if _env("MEDRAG_GEN_CONTEXT"):
    RETRIEVAL["gen_context"] = int(_env("MEDRAG_GEN_CONTEXT"))
if _env("MEDRAG_GEN_CHARS"):
    RETRIEVAL["gen_chars_per_chunk"] = int(_env("MEDRAG_GEN_CHARS"))
if _env("MEDRAG_MIN_CE_SCORE") is not None:
    RETRIEVAL["min_ce_score"] = float(_env("MEDRAG_MIN_CE_SCORE"))
if _env("MEDRAG_GROUNDING_MIN") is not None:
    RETRIEVAL["grounding_min_score"] = float(_env("MEDRAG_GROUNDING_MIN"))
if _env("MEDRAG_SELF_RAG_THRESHOLD") is not None:
    RETRIEVAL["self_rag_grounding_threshold"] = float(_env("MEDRAG_SELF_RAG_THRESHOLD"))

MAX_CHUNKS_PER_BOOK = RETRIEVAL.get("max_chunks_per_book", 2)
RRF_K = RETRIEVAL.get("rrf_k", 60)
RERANK_ENABLED = RETRIEVAL.get("rerank_enabled", True)
QUERY_REWRITE = RETRIEVAL.get("query_rewrite", True)
MULTI_QUERY = RETRIEVAL.get("multi_query", True)
HYDE = RETRIEVAL.get("hyde", False)
MMR_ENABLED = RETRIEVAL.get("mmr_enabled", True)
MMR_LAMBDA = RETRIEVAL.get("mmr_lambda", 0.7)
CONTEXTUAL_HEADERS = RETRIEVAL.get("contextual_headers", True)
PARENT_CHILD = RETRIEVAL.get("parent_child", True)
COMPRESS_CONTEXT = RETRIEVAL.get("compress_context", True)
GROUNDING_CHECK = RETRIEVAL.get("grounding_check", True)
GROUNDING_MIN = float(RETRIEVAL.get("grounding_min_score", 0.45))
MIN_RERANK_SCORE = RETRIEVAL.get("min_rerank_score", 0.22)
MIN_CE_SCORE = float(RETRIEVAL.get("min_ce_score", 0.0))
COLBERT_ENABLED = RETRIEVAL.get("colbert_enabled", True)
COLBERT_TOP_K = RETRIEVAL.get("colbert_top_k", 30)
COLBERT_WEIGHT = RETRIEVAL.get("colbert_weight", 0.35)
SELF_RAG_ENABLED = RETRIEVAL.get("self_rag_enabled", True)
SELF_RAG_MAX_ITERS = RETRIEVAL.get("self_rag_max_iters", 2)
SELF_RAG_THRESHOLD = float(RETRIEVAL.get("self_rag_grounding_threshold", 0.45))
UNLOAD_LOCAL_BEFORE_GENERATE = _env_bool(
    "MEDRAG_UNLOAD_BEFORE_GENERATE",
    bool(RETRIEVAL.get("unload_local_before_generate", True)),
)
INTENT_ROUTING = RETRIEVAL.get("intent_routing", True)
RULE_ENGINE = RETRIEVAL.get("rule_engine", True)
KG_EXPANSION = RETRIEVAL.get("kg_expansion", True)
KG_MAX_FACTS = RETRIEVAL.get("kg_max_facts", 8)

# Only create lightweight working dirs at import — never auto-create multi-GB
# archive stores (those are relocated on demand / by relocate script).
for d in (DATA_DIR, TEXT_OUT, OCR_OUT, REPORTS_DIR, EVAL_DIR, STANDARDS_EXTRACTED):
    d.mkdir(parents=True, exist_ok=True)
# Exam / library may live outside the repo — create only if under ROOT
for d in (EXAM_BOOKS_DIR, LIBRARY_DIR):
    try:
        if ROOT in d.resolve().parents or d.resolve() == ROOT:
            d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
