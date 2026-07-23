"""Full MedicalRAG core test + Triton ASR on user sample m4a files.

Does not modify MedicalRAG or hf_asr. Uses HTTP only.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

DESKTOP = Path.home() / "Desktop"
BACKEND = "http://127.0.0.1:8010"
MEDRAG = "http://127.0.0.1:8080"
TRITON = "http://127.0.0.1:8002"
OUT = Path(__file__).resolve().parents[1] / "reports" / "rag_triton_sample_test.json"

# Filenames may contain narrow no-break spaces from iOS Voice Memos.
SAMPLE_GLOBS = [
    "Feb 3, 5.12*.m4a",
    "Feb 3, 5.15*.m4a",
    "Feb 3, 5.38*.m4a",
]

RAG_CASES = [
    {
        "id": "rag_clinical",
        "query": "What are the first-line antibiotics for community-acquired pneumonia in adults?",
        "specialty": None,
    },
    {
        "id": "rag_radiology",
        "query": "Describe typical chest X-ray findings of lobar pneumonia.",
        "specialty": "radiology",
    },
    {
        "id": "rag_drug",
        "query": "Warfarin INR 4.2 — what are guideline-based next steps?",
        "specialty": None,
    },
]


def find_samples() -> list[Path]:
    found: list[Path] = []
    for g in SAMPLE_GLOBS:
        matches = sorted(DESKTOP.glob(g))
        if matches:
            found.append(matches[0])
    if not found:
        # broader fallback
        found = sorted(DESKTOP.glob("Feb 3*.m4a"))
    return found


def login(client: httpx.Client) -> str:
    r = client.post(
        f"{BACKEND}/api/auth/login",
        data={"username": "admin", "password": "admin"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _safe(s: str) -> str:
    return s.encode("ascii", "replace").decode("ascii")


def main() -> int:
    results: list[dict] = []
    samples = find_samples()
    print(f"Found {len(samples)} sample audio file(s):")
    for p in samples:
        print(f"  - {_safe(p.name)} ({p.stat().st_size} bytes)")

    # --- MedicalRAG health ---
    t0 = time.time()
    try:
        with httpx.Client(timeout=180.0) as c:
            r = c.get(f"{MEDRAG}/health")
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            results.append({
                "test": "medrag_health",
                "result": "PASS" if r.status_code == 200 else "FAIL",
                "seconds": round(time.time() - t0, 1),
                "status": r.status_code,
                "chunks": body.get("chunks"),
                "llm_ok": (body.get("llm") or {}).get("ok") if isinstance(body.get("llm"), dict) else body.get("llm"),
                "embed_ok": (body.get("embeddings") or {}).get("ok") if isinstance(body.get("embeddings"), dict) else None,
            })
            print(f"\n[RAG] /health -> {r.status_code} in {time.time()-t0:.1f}s chunks={body.get('chunks')}")
    except Exception as e:
        results.append({"test": "medrag_health", "result": "FAIL", "error": str(e)})
        print(f"\n[RAG] /health FAIL: {e}")

    # --- Core RAG /ask cases (direct MedicalRAG) ---
    for case in RAG_CASES:
        t0 = time.time()
        payload = {"query": case["query"]}
        if case["specialty"]:
            payload["specialty"] = case["specialty"]
        try:
            with httpx.Client(timeout=900.0) as c:
                r = c.post(f"{MEDRAG}/ask", json=payload)
                data = r.json() if r.status_code == 200 else {"detail": r.text[:500]}
                answer = (data.get("answer") or "") if isinstance(data, dict) else ""
                sources = data.get("sources") if isinstance(data, dict) else None
                ok = r.status_code == 200 and bool(answer.strip())
                results.append({
                    "test": case["id"],
                    "result": "PASS" if ok else "FAIL",
                    "seconds": round(time.time() - t0, 1),
                    "status": r.status_code,
                    "answer_preview": answer[:240],
                    "n_sources": len(sources) if isinstance(sources, list) else 0,
                    "intent": data.get("intent") if isinstance(data, dict) else None,
                })
                print(f"[RAG] {case['id']} -> {'PASS' if ok else 'FAIL'} "
                      f"({time.time()-t0:.0f}s) sources={len(sources) if isinstance(sources, list) else 0}")
                print(f"      {answer[:160].replace(chr(10), ' ')}...")
        except Exception as e:
            results.append({"test": case["id"], "result": "FAIL", "error": str(e)})
            print(f"[RAG] {case['id']} FAIL: {e}")

    # --- aranmed knowledge proxy (if backend up) ---
    t0 = time.time()
    try:
        with httpx.Client(timeout=900.0) as c:
            token = login(c)
            r = c.post(
                f"{BACKEND}/api/knowledge/ask",
                headers={"Authorization": f"Bearer {token}"},
                json={"query": "Summarize indications for chest CT in suspected pneumonia.", "specialty": "radiology"},
            )
            data = r.json() if r.status_code == 200 else {"detail": r.text[:400]}
            answer = ""
            if isinstance(data, dict):
                answer = data.get("answer") or (data.get("medrag") or {}).get("answer") or ""
                if not answer and "detail" not in data:
                    answer = json.dumps(data)[:200]
            ok = r.status_code == 200 and bool(str(answer).strip())
            results.append({
                "test": "aranmed_knowledge_ask",
                "result": "PASS" if ok else "FAIL",
                "seconds": round(time.time() - t0, 1),
                "status": r.status_code,
                "answer_preview": str(answer)[:240],
            })
            print(f"[RAG] aranmed /api/knowledge/ask -> {'PASS' if ok else 'FAIL'} ({time.time()-t0:.0f}s)")
    except Exception as e:
        results.append({"test": "aranmed_knowledge_ask", "result": "FAIL", "error": str(e)})
        print(f"[RAG] aranmed knowledge ask FAIL: {e}")

    # --- Triton ready ---
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{TRITON}/v2/health/ready")
            results.append({
                "test": "triton_ready",
                "result": "PASS" if r.status_code == 200 else "FAIL",
                "status": r.status_code,
            })
            print(f"\n[ASR] Triton ready -> {r.status_code}")
    except Exception as e:
        results.append({"test": "triton_ready", "result": "FAIL", "error": str(e)})
        print(f"\n[ASR] Triton ready FAIL: {e}")

    # --- Transcribe each sample via Triton ---
    if not samples:
        results.append({"test": "triton_samples", "result": "FAIL", "error": "no sample m4a found on Desktop"})
        print("[ASR] No sample files found")
    else:
        with httpx.Client(timeout=600.0) as c:
            for p in samples:
                t0 = time.time()
                try:
                    with p.open("rb") as f:
                        r = c.post(
                            f"{BACKEND}/api/transcribe",
                            files={"file": (p.name, f, "audio/mp4")},
                            data={"model": "whisper-triton"},
                        )
                    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    text = data.get("text", "") if isinstance(data, dict) else ""
                    asr_model = data.get("asr_model") if isinstance(data, dict) else None
                    ok = r.status_code == 200 and asr_model == "whisper-triton"
                    # Empty-ish transcript on garbage audio can still be a pipeline PASS if model path works
                    results.append({
                        "test": f"triton_transcribe::{p.name}",
                        "result": "PASS" if ok else "FAIL",
                        "seconds": round(time.time() - t0, 1),
                        "status": r.status_code,
                        "asr_model": asr_model,
                        "text_len": len(text or ""),
                        "text_preview": (text or "")[:400],
                        "bytes": p.stat().st_size,
                    })
                    print(f"[ASR] Triton {p.name} -> {'PASS' if ok else 'FAIL'} "
                          f"({time.time()-t0:.1f}s) model={asr_model} chars={len(text or '')}")
                    print(f"      {(text or '')[:200].replace(chr(10), ' ')}")
                except Exception as e:
                    results.append({
                        "test": f"triton_transcribe::{p.name}",
                        "result": "FAIL",
                        "error": str(e),
                        "seconds": round(time.time() - t0, 1),
                    })
                    print(f"[ASR] Triton {p.name} FAIL: {e}")

                # Also run default hf_asr for comparison on same file
                t0 = time.time()
                try:
                    with p.open("rb") as f:
                        r = c.post(
                            f"{BACKEND}/api/transcribe",
                            files={"file": (p.name, f, "audio/mp4")},
                        )
                    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    text = data.get("text", "") if isinstance(data, dict) else ""
                    asr_model = data.get("asr_model") if isinstance(data, dict) else None
                    ok = r.status_code == 200 and bool(asr_model)
                    results.append({
                        "test": f"hf_asr_transcribe::{p.name}",
                        "result": "PASS" if ok else "FAIL",
                        "seconds": round(time.time() - t0, 1),
                        "status": r.status_code,
                        "asr_model": asr_model,
                        "text_len": len(text or ""),
                        "text_preview": (text or "")[:400],
                    })
                    print(f"[ASR] hf_asr {p.name} -> {'PASS' if ok else 'FAIL'} "
                          f"({time.time()-t0:.1f}s) model={asr_model} chars={len(text or '')}")
                    print(f"      {(text or '')[:200].replace(chr(10), ' ')}")
                except Exception as e:
                    results.append({
                        "test": f"hf_asr_transcribe::{p.name}",
                        "result": "FAIL",
                        "error": str(e),
                    })
                    print(f"[ASR] hf_asr {p.name} FAIL: {e}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    passed = sum(1 for x in results if x.get("result") == "PASS")
    failed = sum(1 for x in results if x.get("result") == "FAIL")
    print(f"\n=== SUMMARY: {passed} PASS / {failed} FAIL — wrote {OUT} ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
