"""Verify real vLLM (or OpenAI-compat) endpoints + MedicalRAG wiring.

Writes reports/VLLM_WSL_VERIFICATION.md (and JSON samples under reports/).
Uses only env / config — no hardcoded absolute Desktop paths.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

REPORT = ROOT / "reports" / "VLLM_WSL_VERIFICATION.md"
REPORT_JSON = ROOT / "reports" / "vllm_wsl_verification.json"


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


def _get(url: str, timeout: float = 30) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


def _post_json(url: str, payload: dict, timeout: float = 120) -> tuple[int, dict | str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body
    except Exception as e:
        return 0, str(e)


def main() -> int:
    _load_dotenv()
    from medrag.config import (
        EMBED_BASE_URL,
        EMBED_MODEL,
        EMBED_PROVIDER,
        LLM_BASE_URL,
        LLM_MODEL,
        LLM_PROVIDER,
    )

    results: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "llm_provider": LLM_PROVIDER,
        "llm_base_url": LLM_BASE_URL,
        "llm_model": LLM_MODEL,
        "embed_provider": EMBED_PROVIDER,
        "embed_base_url": EMBED_BASE_URL,
        "embed_model": EMBED_MODEL,
        "checks": {},
    }

    # --- probe models ---
    llm_models_url = f"{LLM_BASE_URL.rstrip('/')}/models"
    embed_models_url = f"{EMBED_BASE_URL.rstrip('/')}/models"
    code, body = _get(llm_models_url)
    results["checks"]["llm_models"] = {
        "ok": code == 200,
        "status": code,
        "body_snip": str(body)[:500],
    }
    code, body = _get(embed_models_url)
    results["checks"]["embed_models"] = {
        "ok": code == 200,
        "status": code,
        "body_snip": str(body)[:500],
    }

    # --- embed encode ---
    emb_ok = False
    emb_dim = None
    if EMBED_PROVIDER in ("vllm", "openai"):
        code, resp = _post_json(
            f"{EMBED_BASE_URL.rstrip('/')}/embeddings",
            {"model": EMBED_MODEL, "input": ["hyperkalemia ECG peaked T waves"]},
            timeout=180,
        )
        try:
            emb_dim = len(resp["data"][0]["embedding"])  # type: ignore[index]
            emb_ok = code == 200 and emb_dim and emb_dim > 0
        except Exception:
            emb_ok = False
        results["checks"]["embed_encode"] = {
            "ok": emb_ok,
            "status": code,
            "dim": emb_dim,
            "error": None if emb_ok else str(resp)[:400],
        }
    else:
        results["checks"]["embed_encode"] = {
            "ok": False,
            "skipped": True,
            "reason": f"EMBED_PROVIDER={EMBED_PROVIDER}",
        }

    # --- medrag helpers ---
    try:
        from medrag.index.embedder import embed_health, encode_queries
        from medrag.rag.routing import llm_health

        eh = embed_health()
        lh = llm_health()
        results["checks"]["embed_health"] = {"ok": bool(eh.get("ok", True)), **eh}
        results["checks"]["llm_health"] = {"ok": bool(lh.get("ok", True)), **lh}
        if emb_ok or EMBED_PROVIDER == "local":
            vecs = encode_queries(["ECG findings in hyperkalemia"])
            results["checks"]["encode_queries"] = {
                "ok": True,
                "dim": len(vecs[0]) if vecs else None,
            }
    except Exception as e:
        results["checks"]["medrag_health"] = {"ok": False, "error": str(e)}

    # --- retrieval sample ---
    retrieval = {"ok": False}
    try:
        from medrag.rag.engine import RagEngine

        eng = RagEngine()
        # retrieve-only via answer path may call LLM; prefer retrieval module if available
        try:
            from medrag.rag import retrieval as retmod

            hits = retmod.retrieve("What are the ECG findings in hyperkalemia?", top_k=8)
            retrieval = {
                "ok": bool(hits),
                "n": len(hits) if hits else 0,
                "titles": [
                    (h.get("title") or h.get("source") or "?") for h in (hits or [])[:5]
                ],
            }
        except Exception:
            res = eng.answer("What are the ECG findings in hyperkalemia?")
            retrieval = {
                "ok": bool(res.get("sources")),
                "n_sources": len(res.get("sources") or []),
                "answer_snip": (res.get("answer") or "")[:400],
                "sources": [
                    f"{s.get('title')} p{s.get('page')}" for s in (res.get("sources") or [])[:5]
                ],
            }
            results["checks"]["rag_cli"] = retrieval
        results["checks"]["retrieval"] = retrieval
    except Exception as e:
        results["checks"]["retrieval"] = {"ok": False, "error": str(e)}

    # --- FastAPI health if up ---
    api_host = os.environ.get("MEDRAG_API_HOST", "127.0.0.1")
    api_port = os.environ.get("MEDRAG_API_PORT", "8080")
    # bind address 0.0.0.0 → probe localhost
    probe_host = "127.0.0.1" if api_host in ("0.0.0.0", "::") else api_host
    code, body = _get(f"http://{probe_host}:{api_port}/health", timeout=5)
    results["checks"]["api_health"] = {
        "ok": code == 200,
        "status": code,
        "body_snip": str(body)[:600],
        "note": "optional — start with medrag-serve",
    }

    # Optional RAG answer smoke (real LLM path; may take minutes on 4-bit 8GB)
    skip_rag = os.environ.get("MEDRAG_SKIP_RAG_ANSWER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if skip_rag:
        results["checks"]["rag_answer"] = {
            "ok": None,
            "skipped": True,
            "reason": "MEDRAG_SKIP_RAG_ANSWER",
        }
    else:
        try:
            from medrag.rag.engine import RagEngine

            eng = RagEngine()
            res = eng.answer(
                "What are the ECG findings in hyperkalemia? Reply briefly."
            )
            ans = (res.get("answer") or "").strip()
            srcs = res.get("sources") or []
            results["checks"]["rag_answer"] = {
                "ok": bool(ans),
                "answer_snip": ans[:300],
                "n_sources": len(srcs),
                "titles": [(s.get("title") or "?") for s in srcs[:5]],
            }
        except Exception as e:
            results["checks"]["rag_answer"] = {"ok": False, "error": str(e)[:400]}

    # Detect native vLLM vs shim: Server header / error fingerprints are weak;
    # record whether process is inside WSL by asking user env marker.
    results["platform_note"] = {
        "wsl_distro": os.environ.get("WSL_DISTRO_NAME"),
        "expect_native_vllm": "Run servers via scripts/wsl/serve_qwen35_8gb.sh inside Ubuntu",
    }

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(results, indent=2), encoding="utf-8")

    def flag(ok: bool | None) -> str:
        if ok is True:
            return "PASS"
        if ok is False:
            return "FAIL"
        return "SKIP"

    crit_ok = bool(results["checks"].get("llm_models", {}).get("ok")) and bool(
        results["checks"].get("embed_models", {}).get("ok")
        or results["checks"].get("embed_encode", {}).get("ok")
    )
    overall = "PASS" if crit_ok else "FAIL"

    lines = [
        "# vLLM WSL verification — MedicalRAG",
        "",
        f"**Date (UTC):** {results['ts']}",
        f"**Overall (critical LLM+embed):** **{overall}**",
        f"**LLM:** `{LLM_PROVIDER}` `{LLM_MODEL}` @ `{LLM_BASE_URL}`",
        f"**Embed:** `{EMBED_PROVIDER}` `{EMBED_MODEL}` @ `{EMBED_BASE_URL}`",
        "",
        "## Checks",
        "",
        "| Check | Result | Detail |",
        "|-------|--------|--------|",
    ]
    for name, c in results["checks"].items():
        ok = c.get("ok")
        detail = {k: v for k, v in c.items() if k != "ok"}
        lines.append(f"| `{name}` | **{flag(ok)}** | `{json.dumps(detail)[:180]}` |")

    lines += [
        "",
        "## 8GB notes",
        "",
        "- FP16 Qwen3.5-4B does not fit on 8GB with KV; use `bitsandbytes` + `--language-model-only`.",
        "- Helper: `scripts/wsl/serve_qwen35_8gb.sh` (`VLLM_GPU_MEM_UTIL~0.85`, `max-model-len 2048`).",
        "- Keep embeds on CPU shim `:8001`; dual vLLM LLM+embed OOMs on one 8GB GPU.",
        "- Set `MEDRAG_LLM_MAX_TOKENS` <=256 when `VLLM_MAX_MODEL_LEN=2048` (768 + long RAG -> HTTP 400).",
        "",
        "## How this run was started",
        "",
        "1. NVIDIA driver 610.x + reboot; confirm `nvidia-smi` on Windows and in WSL.",
        "2. WSL: `pip install -U vllm bitsandbytes` then `bash scripts/wsl/serve_qwen35_8gb.sh`.",
        "3. Windows: `scripts/start_qdrant.ps1` + `python -u scripts/serve_openai_embed_windows.py`.",
        "4. App `.env` points at `http://localhost:8000/v1` and `:8001/v1` (env only).",
        "",
        f"Machine JSON: `{REPORT_JSON.relative_to(ROOT).as_posix()}`",
        "",
    ]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    print(REPORT.read_text(encoding="utf-8"))

    critical = [
        results["checks"].get("llm_models", {}).get("ok"),
        results["checks"].get("embed_models", {}).get("ok")
        or results["checks"].get("embed_encode", {}).get("ok"),
    ]
    # Require LLM models at minimum for "real vLLM" path; embed may be sequential/CPU.
    if not results["checks"].get("llm_models", {}).get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
