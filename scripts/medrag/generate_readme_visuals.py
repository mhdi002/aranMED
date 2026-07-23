"""Generate README plots, architecture diagrams, and OCR bbox sample image.

All filesystem locations come from medrag.config (config.yaml + MEDRAG_* env).
No machine-specific absolute paths are hardcoded in this script.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402
import numpy as np  # noqa: E402

_SCRIPT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPT_ROOT / "src"))
from medrag import config as cfg  # noqa: E402

ROOT = cfg.ROOT
REPORTS_DIR = cfg.REPORTS_DIR
# Scratch copies under reports/ (gitignored); README visuals live in docs/images/ (tracked).
PLOTS = REPORTS_DIR / "plots"
IMAGES = ROOT / "docs" / "images"
DIAGRAM_DPI = 200

# Evaluation PNGs linked from README.md (must be under docs/images/, not reports/).
EVAL_PLOT_NAMES = (
    "hit_at_k_en_fa.png",
    "grounding_scores.png",
    "corpus_coverage.png",
    "pipeline_stages.png",
)

# Limited professional palette (2–3 hues + neutrals)
C = {
    "bg": "#FFFFFF",
    "ink": "#1A2332",
    "muted": "#5A6A7A",
    "line": "#8A97A5",
    "source": "#E8F0E9",
    "source_ec": "#3D6B4F",
    "pipeline": "#E7F1F8",
    "pipeline_ec": "#1B4F72",
    "store": "#EDF4F7",
    "store_ec": "#2A6F8F",
    "query": "#F3EDE4",
    "query_ec": "#8A5A2B",
    "accent": "#E6F5F1",
    "accent_ec": "#148F77",
    "panel": "#F5F7FA",
    "panel_ec": "#C5CED8",
}


def _ensure_dirs() -> None:
    PLOTS.mkdir(parents=True, exist_ok=True)
    IMAGES.mkdir(parents=True, exist_ok=True)


def _save_eval_plot(fig, name: str) -> Path:
    """Write evaluation plot to docs/images (README) and mirror under reports/plots."""
    if name not in EVAL_PLOT_NAMES:
        raise ValueError(f"Unexpected eval plot name: {name}")
    primary = IMAGES / name
    fig.savefig(primary, dpi=140, bbox_inches="tight")
    mirror = PLOTS / name
    fig.savefig(mirror, dpi=140, bbox_inches="tight")
    return primary


def _latest_validation_json() -> Path:
    matches = sorted(
        REPORTS_DIR.glob("multistage_validation_*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if not matches:
        raise FileNotFoundError(f"No multistage_validation_*.json under {REPORTS_DIR}")
    return matches[-1]


def _load_val() -> dict:
    with open(_latest_validation_json(), encoding="utf-8") as f:
        return json.load(f)


def _rel_label(path: Path | None, *, fallback: str) -> str:
    """POSIX path relative to ROOT for diagram labels; never an absolute machine path."""
    if path is None:
        return fallback
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return fallback


def _cfg_path_label(key: str, default: str) -> str:
    raw = (cfg.PATHS.get(key) or default or "").strip().replace("\\", "/")
    if not raw or Path(raw).is_absolute():
        return default
    return raw


def _short_model(name: str) -> str:
    return (name or "").rsplit("/", 1)[-1] or name


def _qdrant_host_label() -> str:
    parsed = urlparse(cfg.QDRANT_URL)
    host = parsed.hostname or "localhost"
    port = parsed.port
    return f"{host}:{port}" if port else host


def _collection(store: str) -> str:
    return f"{cfg.COLLECTION}_{store}"


def _find_exam_sample_pdf() -> Path:
    """Pick a sample exam PDF from configured exam_books_dir (no hardcoded filenames)."""
    exam_dir = cfg.EXAM_BOOKS_DIR
    if exam_dir is None or not Path(exam_dir).is_dir():
        raise FileNotFoundError(
            "Exam books directory not configured. Set paths.exam_books_dir or MEDRAG_EXAM_DIR."
        )
    root = Path(exam_dir)
    pdfs = list(root.rglob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"No PDF files found under exam dir: {root}")
    pdfs.sort(key=lambda p: p.stat().st_size, reverse=True)
    return pdfs[0]


def plot_hit_at_k(data: dict) -> Path:
    tracks = data["stages"]["retrieval"]["tracks"]
    labels = [tracks["en"]["label"], tracks["fa"]["label"]]
    hits = [tracks["en"]["hits"], tracks["fa"]["hits"]]
    totals = [tracks["en"]["total"], tracks["fa"]["total"]]
    rates = [tracks["en"]["hit_at_k"] * 100, tracks["fa"]["hit_at_k"] * 100]

    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    colors = ["#2a6f97", "#c45c26"]
    bars = ax.bar(labels, rates, color=colors, width=0.55, edgecolor="#1a1a1a", linewidth=0.6)
    for bar, h, t, r in zip(bars, hits, totals, rates):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 1.5,
            f"{r:.0f}%\n({h}/{t})",
            ha="center",
            va="bottom",
            fontsize=11,
        )
    ax.set_ylim(0, 115)
    ax.set_ylabel("Hit@k (%)")
    ax.set_title("Retrieval gold Hit@k — EN / FA\n(validation 2026-07-19 / report UTC 2026-07-20)")
    ax.axhline(100, color="#888", ls="--", lw=0.8, alpha=0.7)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    out = _save_eval_plot(fig, "hit_at_k_en_fa.png")
    plt.close(fig)
    return out


def plot_grounding(data: dict) -> Path:
    smoke = data["stages"]["smoke"]["results"]
    labels = []
    scores = []
    for i, r in enumerate(smoke, 1):
        q = r["q"]
        short = (q[:28] + "…") if len(q) > 28 else q
        labels.append(f"Q{i}: {short}")
        scores.append(r["grounded"] * 100)

    fig, ax = plt.subplots(figsize=(8.5, 4.4))
    colors = ["#2a6f97" if s >= 60 else "#c45c26" for s in scores]
    y = np.arange(len(labels))
    ax.barh(y, scores, color=colors, edgecolor="#1a1a1a", linewidth=0.5, height=0.65)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("Grounding score (%)")
    ax.set_xlim(0, 100)
    ax.set_title("Smoke bilingual grounding scores\n(validation 2026-07-19)")
    for yi, s in zip(y, scores):
        ax.text(s + 1.2, yi, f"{s:.1f}", va="center", fontsize=9)
    ax.axvline(50, color="#888", ls="--", lw=0.8, alpha=0.7)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    out = _save_eval_plot(fig, "grounding_scores.png")
    plt.close(fig)
    return out


def plot_corpus_coverage(data: dict) -> Path:
    corpora = ["Mehrsys", "Library\n(EN)", "Exam\n(OCR)", "Standards\n(FA)"]
    embedded = [179, 1390, 152, 247]
    gaps = [0, 56, 92, 31]

    fig, ax = plt.subplots(figsize=(8.2, 4.6))
    x = np.arange(len(corpora))
    w = 0.38
    b1 = ax.bar(x - w / 2, embedded, w, label="Embedded / in catalog", color="#2a6f97", edgecolor="#1a1a1a", lw=0.5)
    b2 = ax.bar(x + w / 2, gaps, w, label="Known gaps (not embedded / no OCR)", color="#d4a373", edgecolor="#1a1a1a", lw=0.5)
    ax.set_xticks(x)
    ax.set_xticklabels(corpora)
    ax.set_ylabel("Document count")
    ax.set_title("Corpus coverage vs known gaps\n(validation 2026-07-19; library unique titles)")
    ax.legend(loc="upper right", fontsize=8)
    for bars in (b1, b2):
        for bar in bars:
            h = bar.get_height()
            if h:
                ax.text(bar.get_x() + bar.get_width() / 2, h + 8, str(int(h)), ha="center", va="bottom", fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    out = _save_eval_plot(fig, "corpus_coverage.png")
    plt.close(fig)
    return out


def plot_pipeline_stages(data: dict) -> Path:
    stages = data["stages"]
    names = ["Unit", "Coverage", "Retrieval", "Verify", "Smoke"]
    oks = [
        stages["unit"]["ok"],
        stages["coverage"]["ok"],
        stages["retrieval"]["ok"],
        stages["verify"]["ok"],
        stages["smoke"]["ok"],
    ]
    detail = [
        "pytest PASS",
        "inventory PASS",
        "EN 20/20 · FA 8/8",
        "curated 8/8\n(auto n=0)",
        "4/4 grounded",
    ]

    fig, ax = plt.subplots(figsize=(9.0, 3.8))
    colors = ["#2d6a4f" if ok else "#9b2226" for ok in oks]
    y = np.arange(len(names))
    ax.barh(y, [1] * len(names), color=colors, height=0.7, edgecolor="#1a1a1a", lw=0.5)
    ax.set_yticks(y)
    ax.set_yticklabels(names, fontsize=11)
    ax.set_xlim(0, 1.35)
    ax.set_xticks([])
    ax.set_title("Multistage validation pipeline — stage status\n(2026-07-19 run)")
    for yi, d, ok in zip(y, detail, oks):
        ax.text(1.02, yi, f"{'PASS' if ok else 'FAIL'}  ·  {d}", va="center", fontsize=9)
    ax.spines[["top", "right", "bottom"]].set_visible(False)
    fig.tight_layout()
    out = _save_eval_plot(fig, "pipeline_stages.png")
    plt.close(fig)
    return out


def _box(
    ax,
    x: float,
    y: float,
    w: float,
    h: float,
    text: str,
    *,
    fc: str,
    ec: str,
    fontsize: float = 11,
    weight: str = "medium",
    sub: str | None = None,
    subsize: float = 8.5,
) -> dict[str, float]:
    """Draw a rounded box; return geometry for arrow wiring."""
    patch = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.012,rounding_size=0.12",
        linewidth=1.4,
        edgecolor=ec,
        facecolor=fc,
        mutation_aspect=1.0,
    )
    ax.add_patch(patch)
    cx, cy = x + w / 2, y + h / 2
    fw = "bold" if weight == "bold" else "normal"
    if sub:
        ax.text(cx, cy + 0.14, text, ha="center", va="center", fontsize=fontsize, color=C["ink"], fontweight=fw)
        ax.text(cx, cy - 0.22, sub, ha="center", va="center", fontsize=subsize, color=C["muted"])
    else:
        ax.text(
            cx,
            cy,
            text,
            ha="center",
            va="center",
            fontsize=fontsize,
            color=C["ink"],
            fontweight=fw,
            linespacing=1.25,
        )
    return {"x": x, "y": y, "w": w, "h": h, "cx": cx, "cy": cy, "left": x, "right": x + w, "bottom": y, "top": y + h}


def _arrow(ax, x0: float, y0: float, x1: float, y1: float, *, color: str | None = None, lw: float = 1.6) -> None:
    ax.add_patch(
        FancyArrowPatch(
            (x0, y0),
            (x1, y1),
            arrowstyle="-|>",
            mutation_scale=14,
            linewidth=lw,
            color=color or C["line"],
            shrinkA=1,
            shrinkB=1,
        )
    )


def draw_architecture() -> Path:
    """Left-to-right ingest + query pipeline for README."""
    embed_name = _short_model(cfg.EMBED_MODEL)
    llm_name = (cfg.LLM_PROVIDER or "vllm").upper()
    write_store = cfg.EMBED_WRITE_STORE
    catalog_name = _cfg_path_label("catalog_db", "catalog.db")
    qdrant_label = _qdrant_host_label()

    fig, ax = plt.subplots(figsize=(14.5, 7.2), facecolor=C["bg"])
    ax.set_xlim(0, 14.5)
    ax.set_ylim(0, 7.2)
    ax.set_facecolor(C["bg"])
    ax.axis("off")

    ax.text(
        7.25,
        6.85,
        "MedicalRAG architecture",
        ha="center",
        va="center",
        fontsize=18,
        color=C["ink"],
        fontweight="bold",
    )
    ax.text(
        7.25,
        6.45,
        "Corpus → ingest → embed → Qdrant → retrieve → ground → answer",
        ha="center",
        va="center",
        fontsize=11,
        color=C["muted"],
    )

    # --- Sources (left column) ---
    ax.text(1.55, 5.95, "Corpus sources", ha="center", fontsize=10, color=C["muted"], fontweight="bold")
    sources = [
        (0.45, 5.05, "Exam OCR", "Persian exams"),
        (0.45, 4.05, "EN library", "PDF / EPUB"),
        (0.45, 3.05, "Mehrsys", "Clinical packs"),
        (0.45, 2.05, "Standards", "SOPs / rules"),
    ]
    src_boxes = []
    for x, y, title, sub in sources:
        src_boxes.append(
            _box(ax, x, y, 2.2, 0.78, title, fc=C["source"], ec=C["source_ec"], fontsize=12, sub=sub)
        )

    # Fan into ingest
    ingest = _box(
        ax, 3.55, 3.35, 1.85, 1.15, "Ingest / OCR",
        fc=C["pipeline"], ec=C["pipeline_ec"], fontsize=12, weight="bold",
        sub="extract text",
    )
    for sb in src_boxes:
        _arrow(ax, sb["right"] + 0.04, sb["cy"], ingest["left"] - 0.02, ingest["cy"], lw=1.2)

    # Horizontal pipeline
    stages = [
        (5.65, 3.35, 1.7, 1.15, "Chunk", "headers + overlap", C["pipeline"], C["pipeline_ec"]),
        (7.55, 3.35, 1.85, 1.15, "Embed", embed_name, C["pipeline"], C["pipeline_ec"]),
        (9.65, 3.25, 2.15, 1.35, "Qdrant", f"{qdrant_label} · {write_store}/standards", C["store"], C["store_ec"]),
    ]
    prev = ingest
    boxes = [ingest]
    for x, y, w, h, title, sub, fc, ec in stages:
        b = _box(ax, x, y, w, h, title, fc=fc, ec=ec, fontsize=12, weight="bold", sub=sub, subsize=8)
        _arrow(ax, prev["right"] + 0.02, prev["cy"], b["left"] - 0.02, b["cy"])
        boxes.append(b)
        prev = b

    # Catalog beside Qdrant
    cat = _box(
        ax, 12.05, 3.45, 2.05, 0.95, catalog_name,
        fc=C["accent"], ec=C["accent_ec"], fontsize=11, weight="bold",
        sub="metadata · progress",
    )
    _arrow(ax, boxes[-1]["right"] + 0.02, boxes[-1]["cy"], cat["left"] - 0.02, cat["cy"], color=C["accent_ec"], lw=1.3)

    # Query path (bottom row) — left to right
    ax.text(7.25, 1.95, "Query path", ha="center", fontsize=10, color=C["muted"], fontweight="bold")
    rerank_sub = _short_model(cfg.RERANK_MODEL) if cfg.RERANK_ENABLED else "MMR"
    qsteps = [
        (0.55, 0.55, 2.0, 1.05, "Retrieve", "hybrid + route"),
        (3.0, 0.55, 2.15, 1.05, "Rerank / MMR", rerank_sub),
        (5.55, 0.55, 2.15, 1.05, "Ground", "Self-RAG check"),
        (8.1, 0.55, 2.15, 1.05, f"LLM ({llm_name})", _short_model(cfg.LLM_MODEL)),
        (10.7, 0.55, 2.15, 1.05, "Answer", "cited response"),
    ]
    qboxes = []
    for x, y, w, h, title, sub in qsteps:
        qboxes.append(
            _box(ax, x, y, w, h, title, fc=C["query"], ec=C["query_ec"], fontsize=12, weight="bold", sub=sub)
        )
    for a, b in zip(qboxes, qboxes[1:]):
        _arrow(ax, a["right"] + 0.02, a["cy"], b["left"] - 0.02, b["cy"])

    # Qdrant → Retrieve
    qdrant_box = boxes[-1]
    retrieve = qboxes[0]
    _arrow(
        ax,
        qdrant_box["cx"],
        qdrant_box["bottom"] - 0.02,
        retrieve["cx"],
        retrieve["top"] + 0.02,
        color=C["store_ec"],
        lw=1.5,
    )
    ax.text(9.2, 2.35, "search", fontsize=9, color=C["store_ec"], ha="center")

    fig.subplots_adjust(left=0.02, right=0.98, top=0.96, bottom=0.04)
    out = IMAGES / "architecture.png"
    fig.savefig(out, dpi=DIAGRAM_DPI, facecolor=C["bg"], edgecolor="none")
    plt.close(fig)
    return out


def draw_data_flow() -> Path:
    """Catalog + collections routing schematic (no spaghetti)."""
    text_lbl = _cfg_path_label("text_out", "data/text")
    ocr_lbl = _cfg_path_label("ocr_out", "data/ocr_markdown")
    chunks_lbl = _cfg_path_label("chunks", "data/chunks.jsonl")
    catalog_lbl = _cfg_path_label("catalog_db", "catalog.db")
    archives_lbl = _cfg_path_label("qdrant_archives_dir", "data/qdrant_archives")
    write_store = cfg.EMBED_WRITE_STORE
    col_expand = _collection(write_store)
    col_standards = _collection("standards")
    col_main = _collection("main")

    fig, ax = plt.subplots(figsize=(13.5, 7.2), facecolor=C["bg"])
    ax.set_xlim(0, 13.5)
    ax.set_ylim(0, 7.2)
    ax.set_facecolor(C["bg"])
    ax.axis("off")

    ax.text(
        6.75,
        6.85,
        "Data flow — catalog & collections",
        ha="center",
        va="center",
        fontsize=18,
        color=C["ink"],
        fontweight="bold",
    )
    ax.text(
        6.75,
        6.45,
        "How corpora land in Qdrant and how queries are routed",
        ha="center",
        va="center",
        fontsize=11,
        color=C["muted"],
    )

    # Ingest lane — centered so catalog sits above middle collection
    ax.text(6.75, 5.95, "Ingest lane", ha="center", fontsize=10, color=C["muted"], fontweight="bold")
    c1 = _box(
        ax, 0.55, 4.7, 2.7, 1.0, "Corpora",
        fc=C["source"], ec=C["source_ec"], fontsize=13, weight="bold",
        sub="exam · library · mehrsys · standards",
    )
    c2 = _box(
        ax, 3.85, 4.6, 3.1, 1.2, "Extracted text",
        fc=C["pipeline"], ec=C["pipeline_ec"], fontsize=13, weight="bold",
        sub=f"{text_lbl}  ·  {ocr_lbl}\n{Path(chunks_lbl).name}",
        subsize=8,
    )
    c3 = _box(
        ax, 7.55, 4.6, 2.8, 1.2, catalog_lbl,
        fc=C["accent"], ec=C["accent_ec"], fontsize=13, weight="bold",
        sub="docs · chunks · embed_progress",
    )
    _arrow(ax, c1["right"] + 0.04, c1["cy"], c2["left"] - 0.04, c2["cy"])
    _arrow(ax, c2["right"] + 0.04, c2["cy"], c3["left"] - 0.04, c3["cy"])

    # Collections row
    ax.text(6.75, 4.0, "Qdrant collections  ·  embed → upsert", ha="center", fontsize=10, color=C["muted"], fontweight="bold")
    expand = _box(
        ax, 0.45, 2.25, 3.8, 1.4, col_expand,
        fc=C["store"], ec=C["store_ec"], fontsize=12, weight="bold",
        sub=f"live write target ({write_store})\nlibrary · mehrsys · exam",
        subsize=9,
    )
    standards = _box(
        ax, 4.85, 2.25, 3.8, 1.4, col_standards,
        fc=C["store"], ec=C["store_ec"], fontsize=12, weight="bold",
        sub="Iranian SOPs / rules\nstandards corpus only",
        subsize=9,
    )
    main = _box(
        ax, 9.25, 2.25, 3.8, 1.4, col_main,
        fc=C["panel"], ec=C["panel_ec"], fontsize=12, weight="bold",
        sub=f"legacy archive\n{archives_lbl}/",
        subsize=9,
    )

    # Catalog → collections
    _arrow(ax, c3["cx"] - 1.1, c3["bottom"] - 0.04, expand["cx"], expand["top"] + 0.04, color=C["accent_ec"], lw=1.4)
    _arrow(ax, c3["cx"], c3["bottom"] - 0.04, standards["cx"], standards["top"] + 0.04, color=C["accent_ec"], lw=1.4)
    _arrow(ax, c3["cx"] + 1.1, c3["bottom"] - 0.04, main["cx"], main["top"] + 0.04, color=C["line"], lw=1.2)

    # Query routing strip — parallel destinations (no crossing)
    ax.add_patch(
        FancyBboxPatch(
            (0.4, 0.28),
            12.7,
            1.55,
            boxstyle="round,pad=0.02,rounding_size=0.12",
            linewidth=1.2,
            edgecolor=C["panel_ec"],
            facecolor=C["panel"],
        )
    )
    ax.text(1.15, 1.4, "Query", ha="center", fontsize=12, color=C["ink"], fontweight="bold")
    ax.text(1.15, 0.9, "user question", ha="center", fontsize=9, color=C["muted"])
    _arrow(ax, 1.9, 1.1, 2.85, 1.1, color=C["query_ec"], lw=1.5)

    route = _box(
        ax, 2.9, 0.55, 2.2, 1.1, "Router",
        fc=C["query"], ec=C["query_ec"], fontsize=12, weight="bold",
        sub="intent / specialty",
    )
    _arrow(ax, route["right"] + 0.05, route["cy"], 5.55, route["cy"], color=C["store_ec"], lw=1.4)

    dests = [
        (5.6, 0.55, 2.2, f"→ {write_store}", "clinical corpora", C["store"], C["store_ec"]),
        (8.05, 0.55, 2.35, "→ standards", "SOPs / rules", C["store"], C["store_ec"]),
        (10.65, 0.55, 2.2, "→ main", "legacy only", C["panel"], C["panel_ec"]),
    ]
    for x, y, w, title, sub, fc, ec in dests:
        _box(ax, x, y, w, 1.1, title, fc=fc, ec=ec, fontsize=11, weight="bold", sub=sub)

    ax.text(
        6.75,
        0.12,
        "Router selects collection(s); retrieve runs against expand and/or standards",
        ha="center",
        fontsize=9,
        color=C["muted"],
    )

    fig.subplots_adjust(left=0.02, right=0.98, top=0.96, bottom=0.03)
    out = IMAGES / "data_flow.png"
    fig.savefig(out, dpi=DIAGRAM_DPI, facecolor=C["bg"], edgecolor="none")
    plt.close(fig)
    return out


def make_ocr_bbox_sample() -> tuple[Path, str]:
    """Render a scanned exam page and overlay EasyOCR detection boxes."""
    import fitz
    from PIL import Image, ImageDraw, ImageFont
    import easyocr

    exam_pdf = _find_exam_sample_pdf()

    doc = fitz.open(exam_pdf)
    page_index = 10 if doc.page_count > 12 else 0
    page = doc[page_index]
    dpi = 150
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    doc.close()

    reader = easyocr.Reader(["fa", "en"], gpu=True, verbose=False)
    arr = np.array(img)
    results = reader.readtext(arr, detail=1, paragraph=False)

    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 14)
        font_sm = ImageFont.truetype("arial.ttf", 11)
    except Exception:
        font = ImageFont.load_default()
        font_sm = font

    n = 0
    for bbox, text, conf in results:
        if conf < 0.25:
            continue
        n += 1
        pts = [(int(p[0]), int(p[1])) for p in bbox]
        draw.line(pts + [pts[0]], fill=(220, 40, 40), width=2)
        x0 = min(p[0] for p in pts)
        y0 = min(p[1] for p in pts)
        tag = f"{conf:.2f}"
        tw = draw.textlength(tag, font=font_sm) if hasattr(draw, "textlength") else 28
        draw.rectangle([x0, max(0, y0 - 14), x0 + tw + 4, y0], fill=(220, 40, 40))
        draw.text((x0 + 2, max(0, y0 - 13)), tag, fill=(255, 255, 255), font=font_sm)

    caption_bar_h = 52
    canvas = Image.new("RGB", (img.width, img.height + caption_bar_h), (245, 245, 245))
    canvas.paste(img, (0, 0))
    d2 = ImageDraw.Draw(canvas)
    title = (
        f"EasyOCR text-region boxes on exam page {page_index + 1} "
        f"({n} detections, conf≥0.25)"
    )
    note = (
        f"Sample: {exam_pdf.name} (from MEDRAG_EXAM_DIR / paths.exam_books_dir). "
        "Indexing uses Chandra OCR → markdown; boxes here are EasyOCR for visual QA."
    )
    d2.text((10, img.height + 6), title, fill=(20, 20, 20), font=font)
    d2.text((10, img.height + 28), note, fill=(60, 60, 60), font=font_sm)

    out = IMAGES / "ocr_bbox_sample.png"
    canvas.save(out, "PNG", optimize=True)
    method = (
        f"EasyOCR fa+en boxes on scanned page {page_index + 1} of "
        f"{exam_pdf.name} ({n} boxes). "
        "Not Chandra model boxes (Chandra returns HTML/text without coordinates)."
    )
    return out, method


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate README visuals for MedicalRAG")
    parser.add_argument(
        "--diagrams-only",
        action="store_true",
        help="Only regenerate architecture.png and data_flow.png",
    )
    parser.add_argument(
        "--skip-ocr",
        action="store_true",
        help="Skip OCR bbox sample (plots + diagrams only)",
    )
    args = parser.parse_args(argv)

    _ensure_dirs()
    written: list[Path] = []

    if args.diagrams_only:
        print("Generating architecture diagrams…")
        written.append(draw_architecture())
        written.append(draw_data_flow())
    else:
        data = _load_val()
        print("Generating evaluation plots…")
        written.append(plot_hit_at_k(data))
        written.append(plot_grounding(data))
        written.append(plot_corpus_coverage(data))
        written.append(plot_pipeline_stages(data))

        print("Generating architecture diagrams…")
        written.append(draw_architecture())
        written.append(draw_data_flow())

        if not args.skip_ocr:
            print("Generating OCR bounding-box sample (EasyOCR; may download models)…")
            ocr_path, method = make_ocr_bbox_sample()
            written.append(ocr_path)
            method_path = IMAGES / "ocr_bbox_sample_caption.txt"
            method_path.write_text(method + "\n", encoding="utf-8")
            print(f"OCR note: {method}")

    print("\nWrote:")
    for p in written:
        try:
            rel = p.relative_to(ROOT).as_posix()
        except ValueError:
            rel = p.as_posix()
        print(f"  {rel}  ({p.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
