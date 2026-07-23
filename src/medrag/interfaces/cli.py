"""CLI for medical RAG queries."""
import argparse

from medrag.rag.engine import RagEngine


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?", default=None)
    ap.add_argument("--image", default=None, help="clinical image to interpret")
    ap.add_argument("--qimage", default=None, help="exam question image to OCR+answer")
    ap.add_argument("--specialty", default=None)
    args = ap.parse_args()

    eng = RagEngine()

    if args.qimage:
        out = eng.answer_question_image(args.qimage)
        print("\n=== OCR'd text ===\n" + out["ocr_text"][:800])
        for i, r in enumerate(out["results"], 1):
            print(f"\n===== Q{i} =====\n{r['question'][:300]}")
            print("\n--- Answer ---\n" + r["answer"])
            for s in r["sources"]:
                print(f"  [{s['n']}] {s['title']} p{s['page']}")
        return

    res = eng.answer(args.question, image_path=args.image, specialty=args.specialty)
    if res.get("image_desc"):
        print("\n[Image analysis]\n" + res["image_desc"])
    if res.get("intent"):
        print(f"\n[Intent: {res['intent']}]")
    for a in res.get("rule_alerts") or []:
        print(f"[Rule {a.get('severity')}] {a.get('message_fa') or a.get('message')}")
    for f in (res.get("kg_facts") or [])[:5]:
        print(f"[KG] {f.get('fact')}")
    print("\n=== Answer ===\n" + res["answer"])
    g = res.get("grounding", {})
    if g:
        print(f"\n[Grounding: {g.get('confidence', '?')} score={g.get('grounded', '?')}]")
    sr = res.get("self_rag", {})
    if sr.get("iterations"):
        print(f"[Self-RAG: {sr.get('final_action', '?')} after {len(sr['iterations'])} step(s)]")
    print("\n=== Sources ===")
    for s in res["sources"]:
        corpus = s.get("source_corpus") or ""
        print(f"  [{s['n']}] {s['title']} p{s['page']} ({s['specialty']}) {corpus}")


if __name__ == "__main__":
    main()
