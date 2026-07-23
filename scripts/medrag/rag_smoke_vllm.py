"""Quick RAG answer smoke against env-configured LLM/embed (no Desktop hardcodes)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_dotenv() -> None:
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def main() -> int:
    _load_dotenv()
    # Force re-read of config after dotenv (import after load)
    from medrag.rag.engine import RagEngine
    from medrag import config as cfg

    print(
        f"LLM={cfg.LLM_PROVIDER} model={cfg.LLM_MODEL} url={cfg.LLM_BASE_URL} "
        f"max_tokens={cfg.LLM_MAX_TOKENS} think={cfg.LLM_ENABLE_THINKING}",
        flush=True,
    )
    eng = RagEngine()
    res = eng.answer("What are the ECG findings in hyperkalemia? Reply briefly.")
    ans = (res.get("answer") or "").strip()
    srcs = res.get("sources") or []
    print("ANSWER_OK", bool(ans), flush=True)
    print("ANSWER_SNIP", ans[:600], flush=True)
    print("N_SOURCES", len(srcs), flush=True)
    print(
        "SOURCES",
        [f"{s.get('title')} p{s.get('page')}" for s in srcs[:5]],
        flush=True,
    )
    return 0 if ans else 1


if __name__ == "__main__":
    raise SystemExit(main())
