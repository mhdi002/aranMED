"""OCR quality evaluation on curated test images."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from medrag.config import EVAL_DIR, REPORTS_DIR
from medrag.rag.multimodal import ocr_image


def term_recall(text: str, must_any: list[str]) -> float:
    text_l = text.lower()
    hits = sum(1 for t in must_any if t.lower() in text_l)
    return hits / len(must_any) if must_any else 0.0


def run_ocr_eval(samples_dir: Path | None = None) -> dict:
    samples_dir = samples_dir or EVAL_DIR / "ocr_samples"
    manifest = samples_dir / "manifest.jsonl"
    if not manifest.exists():
        return {"error": f"No manifest at {manifest}", "samples": 0}

    results = []
    for line in open(manifest, encoding="utf-8"):
        spec = json.loads(line)
        img = samples_dir / spec["image"]
        if not img.exists():
            results.append({**spec, "status": "missing_image", "recall": 0})
            continue
        text = ocr_image(str(img))
        recall = term_recall(text, spec.get("must_any", []))
        passed = recall >= spec.get("min_recall", 0.85)
        results.append({
            "image": spec["image"],
            "recall": round(recall, 3),
            "passed": passed,
            "text_preview": text[:300],
            "must_any": spec.get("must_any", []),
        })
        mark = "PASS" if passed else "FAIL"
        print(f"[{mark}] {spec['image']} recall={recall:.1%}")

    avg = sum(r["recall"] for r in results if "recall" in r) / max(len(results), 1)
    summary = {
        "samples": len(results),
        "avg_term_recall": round(avg, 3),
        "passed": sum(1 for r in results if r.get("passed")),
        "details": results,
    }
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", default=None)
    args = ap.parse_args()
    summary = run_ocr_eval(Path(args.samples) if args.samples else None)
    out = REPORTS_DIR / "ocr_eval_latest.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nOCR eval: {summary.get('passed', 0)}/{summary.get('samples', 0)} passed")
    print(f"Report: {out}")


if __name__ == "__main__":
    main()
