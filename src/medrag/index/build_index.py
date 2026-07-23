"""Embed chunks into Qdrant with bge-m3 — resumable batch checkpoints."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from qdrant_client import models

from medrag.catalog.documents import (
    clear_embed_progress, exam_content_hash, get_embed_resume, hash_file,
    init_schema, is_fully_indexed, mark_indexed, purge_stale_vectors,
    remove_book, save_embed_progress,
)
from medrag.config import CATALOG_DB, CONTEXTUAL_HEADERS, EMBED_BATCH, MIN_CHUNK_CHARS
from medrag.index import embedder, vectorstore as vs
from medrag.index._compat import patch_bge_m3_sparse
from medrag.ingest.chunk import chunk_library_file, load_pages, split_qbank, split_textbook
from medrag.ingest.contextual import attach_parent_text, build_page_map_from_pages, embed_text
from medrag.catalog.registry import load_manifest, scan_library

patch_bge_m3_sparse()


def _embed_texts_safe(texts: list[str]) -> list[dict]:
    """Encode with config EMBED_BATCH (falls back inside embedder on OOM)."""
    return embedder.encode_passages(texts, batch_size=EMBED_BATCH)


def _book_id_exam(stem: str) -> str:
    return f"exam:{stem}"


def _doc_hash(source: str, content_hash: str) -> str:
    return f"{source}:{content_hash[:24]}"


def _point_id(doc_hash: str, chunk_index: int) -> str:
    """Stable ID — re-upsert on resume overwrites instead of duplicating."""
    h = hashlib.md5(f"{doc_hash}:{chunk_index}".encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def guess_year(name: str) -> int | None:
    m = re.search(r"(20\d{2}|13[8-9]\d|14[0-1]\d)", name)
    if not m:
        return None
    y = int(m.group(1))
    if y > 1500:
        return y
    # Persian calendar rough map → Gregorian for storage
    return y + 621 if y < 1500 else y


def index_tags_for(doc_type: str, name: str = "") -> list[str]:
    tags = [doc_type]
    n = name.lower()
    if any(k in n for k in ("drug", "دارو", "pharm", "دارویی")):
        tags.append("drug")
    if any(k in n for k in ("icd", "کدینگ", "coding")):
        tags.append("icd_candidate")
    if any(k in n for k in ("lab", "آزمایش", "mri", "ct", "imaging", "تصویر")):
        tags.append("imaging" if "imag" in n or "تصویر" in n or "mri" in n else "lab")
    if doc_type == "legal":
        tags.append("legal")
    elif doc_type in ("guideline", "standard"):
        tags.append("guideline")
    return list(dict.fromkeys(tags))


def upsert_chunks(chunks: list[dict], doc_hash: str, title: str, specialty: str,
                  language: str, doc_type: str, source_corpus: str,
                  conn: sqlite3.Connection, book_id: str, content_hash: str,
                  file_path: str = "") -> int:
    """Embed in batches with SQLite checkpoint — safe to Ctrl+C and resume."""
    if not chunks:
        return 0

    from medrag.config import EMBED_WRITE_STORE, QDRANT_MODE
    qstore = (
        "standards" if source_corpus == "standards"
        else (EMBED_WRITE_STORE if EMBED_WRITE_STORE in ("expand", "expand2", "expand3") else "expand")
        if source_corpus in ("mehrsys", "library", "exam")
        else "main"
    )
    vs.ensure_collection(qstore)
    print(
        f"    [qdrant] mode={QDRANT_MODE} store={qstore} "
        f"collection={vs.collection_name(qstore)}",
        flush=True,
    )

    batch_size = max(1, int(EMBED_BATCH))
    # Large parent_text payloads blow Qdrant's ~32MB JSON limit at 48 pts;
    # start small and adapt down further on payload errors.
    import os
    upsert_size = max(1, min(int(os.environ.get("MEDRAG_UPSERT_SIZE", "8")), batch_size))
    total = len(chunks)
    start = get_embed_resume(conn, book_id, content_hash, doc_hash,
                             source_corpus=source_corpus)

    if start == 0:
        rec = conn.execute(
            "SELECT doc_hash FROM documents WHERE book_id=?", (book_id,),
        ).fetchone()
        if rec and rec[0] and rec[0] != doc_hash:
            vs.delete_by_doc(rec[0], store=qstore)
        vs.delete_by_doc(doc_hash, store=qstore)
        clear_embed_progress(conn, book_id)
    elif start >= total:
        clear_embed_progress(conn, book_id)
        mark_indexed(conn, book_id, content_hash, doc_hash, title, specialty,
                     file_path, language, doc_type, source_corpus, total)
        return total

    if start > 0:
        print(f"    [resume] {title[:40]:40s} chunk {start}/{total}", flush=True)
    print(
        f"    [batch] embed_batch_size={batch_size} upsert_size={upsert_size}",
        flush=True,
    )

    def _upsert_adaptive(slice_pts: list, base_idx: int) -> int:
        """Upsert points; split on Qdrant payload-too-large. Returns chunks done."""
        size = min(upsert_size, len(slice_pts))
        offset = 0
        while offset < len(slice_pts):
            n = min(size, len(slice_pts) - offset)
            part = slice_pts[offset:offset + n]
            try:
                vs.upsert(part, store=qstore)
            except Exception as e:
                err = str(e)
                if n > 1 and ("larger than allowed" in err or "Payload error" in err):
                    size = max(1, n // 2)
                    print(
                        f"    [upsert] payload too large at {base_idx + offset}; "
                        f"retry upsert_size={size}",
                        flush=True,
                    )
                    continue
                save_embed_progress(
                    conn, book_id, content_hash, doc_hash, total, base_idx + offset,
                )
                embedder._reset_embedder()
                raise RuntimeError(
                    f"upsert aborted at chunk {base_idx + offset}/{total} "
                    f"for {title[:60]}: {e}"
                ) from e
            offset += n
            done = base_idx + offset
            save_embed_progress(conn, book_id, content_hash, doc_hash, total, done)
            print(f"    {title[:40]:40s} {done:5d}/{total} chunks", flush=True)
        return base_idx + len(slice_pts)

    for i in range(start, total, batch_size):
        end = min(i + batch_size, total)
        batch = chunks[i:end]
        texts = [c.get("embed_text") or c["text"] for c in batch]
        try:
            vecs = _embed_texts_safe(texts)
        except Exception as e:
            # Checkpoint at batch start so resume retries the failed window
            save_embed_progress(conn, book_id, content_hash, doc_hash, total, i)
            embedder._reset_embedder()
            raise RuntimeError(
                f"embed aborted at chunk {i}/{total} for {title[:60]}: {e}"
            ) from e

        points = []
        for j, (c, vec) in enumerate(zip(batch, vecs)):
            idx = i + j
            page = c.get("page") or c.get("page_start", 0)
            tags = c.get("index_tags") or [doc_type]
            if isinstance(tags, str):
                tags = [tags]
            parent = c.get("parent_text", c["text"]) or c["text"]
            # Cap parent context so a single point cannot dominate the HTTP body
            if isinstance(parent, str) and len(parent) > 12000:
                parent = parent[:12000]
            points.append(models.PointStruct(
                id=_point_id(doc_hash, idx),
                vector={"dense": vec["dense"], "sparse": vs.to_sparse(vec["sparse"])},
                payload={
                    "text": c["text"],
                    "parent_text": parent,
                    "title": title,
                    "book": c.get("book", title),
                    "specialty": specialty,
                    "language": language,
                    "doc_type": doc_type,
                    "source_corpus": source_corpus,
                    "page": page,
                    "page_start": c.get("page_start", page),
                    "page_end": c.get("page_end", page),
                    "doc_hash": doc_hash,
                    "book_id": book_id,
                    "content_hash": content_hash,
                    "chunk_id": c.get("chunk_id") or c.get("id"),
                    "cko_id": c.get("cko_id"),
                    "section_title": c.get("section_title"),
                    "topic": c.get("topic"),
                    "country": c.get("country"),
                    "year": c.get("year"),
                    "version": c.get("version"),
                    "index_tags": tags,
                    "active": bool(c.get("active", True)),
                    "evidence_level": c.get("evidence_level"),
                    "recommendation_class": c.get("recommendation_class"),
                    "population": c.get("population"),
                },
            ))

        _upsert_adaptive(points, i)

    mark_indexed(conn, book_id, content_hash, doc_hash, title, specialty,
                 file_path, language, doc_type, source_corpus, total)
    return total


def embed_exam_corpus(conn, book_filter: str | None = None) -> int:
    import os
    rows = load_manifest()
    total = 0
    for rec in rows:
        if rec.get("duplicate") or rec.get("excluded"):
            continue
        if book_filter and rec["file"] != book_filter:
            continue
        stem = os.path.splitext(rec["file"])[0]
        if not load_pages(stem):
            continue
        book_id = _book_id_exam(stem)
        content_hash = exam_content_hash(stem, rec.get("path"))
        if not content_hash:
            continue
        if is_fully_indexed(conn, book_id, content_hash):
            print(f"[skip] {rec['file'][:50]} (done)")
            continue

        pages = load_pages(stem)
        if rec["type"] in ("qbank", "exam"):
            spans = split_qbank(pages)
        else:
            spans = split_textbook(pages)
        raw_chunks = [{"text": t, "page": ps, "page_start": ps, "page_end": pe, "book": rec["file"]}
                      for ps, pe, t in spans if t and len(t) >= MIN_CHUNK_CHARS]
        page_map = build_page_map_from_pages(pages)
        chunks = []
        for c in raw_chunks:
            meta = {
                "title": rec["file"], "book": rec["file"],
                "specialty": rec["specialty"], "language": rec["language"],
                "doc_type": rec["type"],
                "page_start": c["page_start"], "page_end": c["page_end"], "page": c["page"],
            }
            c = {**c, "embed_text": embed_text(c["text"], meta, CONTEXTUAL_HEADERS)}
            chunks.append(c)
        chunks = attach_parent_text(chunks, page_map)
        doc_hash = _doc_hash("exam", content_hash)
        n = upsert_chunks(chunks, doc_hash, rec["file"], rec["specialty"],
                          rec["language"], rec["type"], "exam", conn,
                          book_id, content_hash, rec.get("path", ""))
        total += n
        print(f"[ok] exam {rec['file'][:50]:50s} -> {n} chunks")
    return total


def embed_library(conn, book_filter: str | None = None,
                  source_corpus_filter: str | None = None,
                  max_books: int | None = None) -> int:
    from medrag.ingest.mehrsys import is_mehrsys_pack, pack_content_hash, pack_dir

    total = 0
    done_books = 0
    for entry in scan_library():
        corpus = entry.get("source_corpus", "library")
        if source_corpus_filter and corpus != source_corpus_filter:
            continue
        if book_filter and entry["title"] != book_filter and Path(entry["file_path"]).name != book_filter:
            continue
        path = Path(entry["file_path"])
        if not path.exists():
            continue
        if is_mehrsys_pack(path):
            folder = pack_dir(path)
            content_hash = pack_content_hash(folder)
            stem = folder.name
            book_id = f"mehrsys:{entry['specialty']}:{stem}"
            source_tag = "mehrsys"
            lang = entry.get("language", "en")
        elif corpus == "standards":
            content_hash = hash_file(path)
            stem = path.stem
            book_id = f"std:{entry['specialty']}:{stem}"
            source_tag = "std"
            lang = entry.get("language", "fa")
        else:
            content_hash = hash_file(path)
            stem = path.stem
            book_id = f"lib:{entry['specialty']}:{stem}"
            source_tag = "lib"
            lang = entry.get("language", "en")
        if is_fully_indexed(conn, book_id, content_hash):
            print(f"[skip] {entry['title'][:50]} (done)")
            continue
        year = entry.get("year") or guess_year(entry["title"] + " " + path.name)
        tags = entry.get("index_tags") or index_tags_for(entry.get("doc_type", "textbook"), entry["title"])
        try:
            chunks = chunk_library_file(
                path, entry["title"], entry["specialty"],
                language=lang,
                doc_type=entry.get("doc_type", "textbook"),
                source_corpus=corpus,
                country=entry.get("country") or ("IR" if corpus == "standards" else None),
                index_tags=tags,
                year=year,
            )
            chunks = [c for c in chunks if len(c["text"]) >= MIN_CHUNK_CHARS]
            doc_hash = _doc_hash(source_tag, content_hash)
            n = upsert_chunks(chunks, doc_hash, entry["title"], entry["specialty"],
                              lang, entry["doc_type"], corpus, conn,
                              book_id, content_hash, str(path))
            total += n
            done_books += 1
            label = {"mehrsys": "mehrsys", "standards": "std", "library": "lib"}.get(corpus, corpus)
            status = "ok" if n > 0 else "warn"
            print(f"[{status}] {label:7s} {entry['title'][:50]:50s} -> {n} chunks")
            if max_books and done_books >= max_books:
                break
        except Exception as e:
            embedder._reset_embedder()
            print(f"[fail] {entry['title'][:50]} -- {type(e).__name__}: {e!r}")
            # leave progress so resume retries the failed chunk; do not mark_indexed
            continue
    return total


def run_embed(corpus: str = "all", cleanup: bool = False,
              book: str | None = None,
              source_corpus_filter: str | None = None,
              max_books: int | None = None) -> dict:
    from medrag.config import EMBED_WRITE_STORE, QDRANT_MODE
    write_store = EMBED_WRITE_STORE if EMBED_WRITE_STORE in ("expand", "expand2", "expand3") else "expand"
    # Only open the store(s) we will write — never load local main/expand (OOM)
    if source_corpus_filter == "standards":
        write_store = "standards"
    elif corpus == "exam" or source_corpus_filter in ("mehrsys", "library", None):
        # exam / mehrsys / library / all → server expand (or EMBED_WRITE_STORE)
        if write_store == "main":
            raise RuntimeError("refusing to embed into main store (OOM risk); set embed_write_store=expand")
    vs.ensure_collection(write_store)
    print(
        f"[run_embed] mode={QDRANT_MODE} corpus={corpus} filter={source_corpus_filter} "
        f"store={write_store} collection={vs.collection_name(write_store)}",
        flush=True,
    )
    conn = sqlite3.connect(CATALOG_DB)
    init_schema(conn)
    stats = {"exam": 0, "library": 0, "purged": 0}
    if corpus in ("all", "exam"):
        stats["exam"] = embed_exam_corpus(conn, book_filter=book)
    if corpus in ("all", "library"):
        stats["library"] = embed_library(
            conn, book_filter=book,
            source_corpus_filter=source_corpus_filter,
            max_books=max_books,
        )
    if cleanup:
        stats["purged"] = purge_stale_vectors(conn)
    # Do not call vs.count() across all stores — opening main/expand OOMs in local mode
    try:
        stats["total_chunks"] = vs.count(write_store)
    except Exception as e:
        stats["total_chunks"] = f"unavailable:{type(e).__name__}"
    conn.close()
    return stats


def reindex_book(book_id: str) -> int:
    conn = sqlite3.connect(CATALOG_DB)
    init_schema(conn)
    remove_book(conn, book_id)
    clear_embed_progress(conn, book_id)
    conn.close()
    if book_id.startswith("exam:"):
        return run_embed(corpus="exam", cleanup=True)["exam"]
    return run_embed(corpus="library", cleanup=True)["library"]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Resumable embed into Qdrant")
    ap.add_argument("--corpus", choices=["all", "exam", "library"], default="all")
    ap.add_argument("--book", default=None, help="single book filename to embed/resume")
    ap.add_argument("--no-cleanup", action="store_true")
    args = ap.parse_args()
    print(run_embed(args.corpus, cleanup=not args.no_cleanup, book=args.book))
