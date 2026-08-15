"""Qdrant hybrid collections (bge-m3 dense + sparse vectors).

Server mode (preferred): one Qdrant process on :6333; logical stores map to
separate collection names (never load disk indexes into the Python process).

Local mode (legacy): each store is a separate on-disk path — grows until OOM.
"""
from __future__ import annotations

import warnings

from qdrant_client import QdrantClient, models

from medrag.config import (
    COLLECTION, DENSE_DIM, EMBED_WRITE_STORE, QDRANT_EXPAND2_STORAGE,
    QDRANT_EXPAND3_STORAGE, QDRANT_EXPAND_STORAGE, QDRANT_MODE,
    QDRANT_STORE_NAMES, QDRANT_STANDARDS_STORAGE, QDRANT_STORAGE, QDRANT_URL,
    RRF_K,
)

# Logical stores are configuration, not a literal: a deployment whose corpus
# only populated main/standards/expand must not be forced to probe collections
# that were never created. Override with MEDRAG_QDRANT_STORES (see
# docs/core/CONFIGURATION.md); config.py holds the default.
STORE_NAMES = QDRANT_STORE_NAMES

_clients: dict[str, QdrantClient | None] = {s: None for s in STORE_NAMES}
_server_client: QdrantClient | None = None

_STORE_PATHS = {
    "main": QDRANT_STORAGE,
    "standards": QDRANT_STANDARDS_STORAGE,
    "expand": QDRANT_EXPAND_STORAGE,
    "expand2": QDRANT_EXPAND2_STORAGE,
    "expand3": QDRANT_EXPAND3_STORAGE,
}


def collection_name(store: str = "main") -> str:
    """Map logical store → Qdrant collection name.

    Server: medical_library_main, medical_library_expand, …
    Local: single name per path (COLLECTION), paths isolate data.
    """
    if store not in STORE_NAMES:
        raise ValueError(f"unknown qdrant store: {store}")
    if QDRANT_MODE == "server":
        return f"{COLLECTION}_{store}"
    return COLLECTION


def _open_local(path) -> QdrantClient:
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=".*Local mode is not recommended.*",
                category=UserWarning,
            )
            return QdrantClient(path=str(path))
    except RuntimeError as e:
        if "already accessed" in str(e):
            lock = path / ".lock"
            if lock.exists():
                lock.unlink(missing_ok=True)
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=".*Local mode is not recommended.*",
                    category=UserWarning,
                )
                return QdrantClient(path=str(path))
        raise


def require_server(timeout: float = 3.0) -> None:
    """Fail loudly if Qdrant server is down (no silent hang / local fallback)."""
    if QDRANT_MODE != "server":
        return
    import urllib.error
    import urllib.request

    # QDRANT_URL is required at import time (config.py raises if unset), so
    # there is no literal fallback host here by design.
    url = QDRANT_URL.rstrip("/") + "/collections"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Qdrant HTTP {resp.status} at {url}")
    except Exception as e:
        raise RuntimeError(
            f"Qdrant server is not reachable at {QDRANT_URL} "
            "(set QDRANT_URL / vector_db.url).\n"
            "Start it, then re-run embed:\n"
            "  docker compose up -d qdrant\n"
            f"Underlying error: {type(e).__name__}: {e}"
        ) from e


def client(store: str = "main") -> QdrantClient:
    """Return Qdrant client for store: main | standards | expand | expand2 | expand3."""
    global _clients, _server_client
    if store not in _clients:
        raise ValueError(f"unknown qdrant store: {store}")

    if QDRANT_MODE == "server":
        require_server()
        if _server_client is None:
            _server_client = QdrantClient(
                url=QDRANT_URL, timeout=120, check_compatibility=False,
            )
        _clients[store] = _server_client
        return _server_client

    if _clients.get(store) is None:
        _clients[store] = _open_local(_STORE_PATHS[store])
    return _clients[store]


def _store_has_points(store: str) -> bool:
    try:
        c = client(store)
        name = collection_name(store)
        return c.collection_exists(name) and c.count(name).count > 0
    except Exception:
        return False


def _stores_for_search(source_corpus=None) -> list[str]:
    extras = ("expand3", "expand2", "expand", "standards")
    if source_corpus == "standards":
        return ["standards"]
    if source_corpus == "mehrsys":
        stores = [s for s in ("expand3", "expand2", "expand", "main") if _store_has_points(s)]
        return stores or [EMBED_WRITE_STORE]
    stores = ["main"]
    for extra in extras:
        if _store_has_points(extra):
            stores.append(extra)
    return stores


def ensure_collection(store: str = "main"):
    if QDRANT_MODE == "server":
        require_server()
    c = client(store)
    name = collection_name(store)
    if c.collection_exists(name):
        return
    c.create_collection(
        collection_name=name,
        vectors_config={
            "dense": models.VectorParams(
                size=DENSE_DIM,
                distance=models.Distance.COSINE,
                on_disk=True,
            )
        },
        sparse_vectors_config={
            "sparse": models.SparseVectorParams(
                index=models.SparseIndexParams(on_disk=True)
            )
        },
        quantization_config=models.ScalarQuantization(
            scalar=models.ScalarQuantizationConfig(
                type=models.ScalarType.INT8, always_ram=False
            )
        ),
        optimizers_config=models.OptimizersConfigDiff(default_segment_number=4),
    )
    for field in ("specialty", "doc_hash", "language", "doc_type", "source_corpus",
                  "country", "topic", "chunk_id", "cko_id"):
        try:
            c.create_payload_index(name, field, models.PayloadSchemaType.KEYWORD)
        except Exception:
            pass
    for field in ("year",):
        try:
            c.create_payload_index(name, field, models.PayloadSchemaType.INTEGER)
        except Exception:
            pass
    for field in ("active",):
        try:
            c.create_payload_index(name, field, models.PayloadSchemaType.BOOL)
        except Exception:
            pass
    try:
        c.create_payload_index(name, "index_tags", models.PayloadSchemaType.KEYWORD)
    except Exception:
        pass


def ensure_payload_indexes():
    """Add new indexes on an existing collection (idempotent)."""
    for store in STORE_NAMES:
        try:
            c = client(store)
            name = collection_name(store)
            if not c.collection_exists(name):
                continue
        except Exception:
            continue
        for field, schema in (
            ("specialty", models.PayloadSchemaType.KEYWORD),
            ("doc_hash", models.PayloadSchemaType.KEYWORD),
            ("language", models.PayloadSchemaType.KEYWORD),
            ("doc_type", models.PayloadSchemaType.KEYWORD),
            ("source_corpus", models.PayloadSchemaType.KEYWORD),
            ("country", models.PayloadSchemaType.KEYWORD),
            ("topic", models.PayloadSchemaType.KEYWORD),
            ("chunk_id", models.PayloadSchemaType.KEYWORD),
            ("cko_id", models.PayloadSchemaType.KEYWORD),
            ("index_tags", models.PayloadSchemaType.KEYWORD),
            ("year", models.PayloadSchemaType.INTEGER),
            ("active", models.PayloadSchemaType.BOOL),
        ):
            try:
                c.create_payload_index(name, field, schema)
            except Exception:
                pass


def to_sparse(sp: dict) -> models.SparseVector:
    """Build sparse vector for upsert. Empty → tiny placeholder (collection requires sparse)."""
    indices, values = [], []
    for k, v in (sp or {}).items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx > 169511:
            continue
        fv = float(v)
        if fv <= 0:
            continue
        indices.append(idx)
        values.append(fv)
    if not indices:
        indices, values = [0], [1e-12]
    return models.SparseVector(indices=indices, values=values)


def sparse_query_vector(sp: dict) -> models.SparseVector | None:
    """Query-time sparse vector; None when empty (skip sparse leg)."""
    indices, values = [], []
    for k, v in (sp or {}).items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx > 169511:
            continue
        fv = float(v)
        if fv <= 0:
            continue
        indices.append(idx)
        values.append(fv)
    if not indices:
        return None
    return models.SparseVector(indices=indices, values=values)


def upsert(points, store: str = "main"):
    client(store).upsert(collection_name=collection_name(store), points=points, wait=True)


def delete_by_doc(doc_hash: str, store: str = "main"):
    client(store).delete(
        collection_name=collection_name(store),
        points_selector=models.FilterSelector(
            filter=models.Filter(must=[models.FieldCondition(
                key="doc_hash", match=models.MatchValue(value=doc_hash))])
        ),
    )


def count(store: str | None = None) -> int:
    if store:
        name = collection_name(store)
        return client(store).count(name).count
    total = 0
    for s in STORE_NAMES:
        try:
            c = client(s)
            name = collection_name(s)
            if c.collection_exists(name):
                total += c.count(name).count
        except Exception:
            pass
    return total


def _build_filter(specialties=None, language=None, source_corpus=None,
                  doc_types=None, index_tags=None, country=None, active=None):
    must = []
    if specialties:
        must.append(models.FieldCondition(
            key="specialty", match=models.MatchAny(any=specialties)))
    if language:
        must.append(models.FieldCondition(
            key="language", match=models.MatchValue(value=language)))
    if source_corpus:
        corpora = source_corpus if isinstance(source_corpus, list) else [source_corpus]
        must.append(models.FieldCondition(
            key="source_corpus", match=models.MatchAny(any=corpora)))
    if doc_types:
        must.append(models.FieldCondition(
            key="doc_type", match=models.MatchAny(any=doc_types)))
    if index_tags:
        must.append(models.FieldCondition(
            key="index_tags", match=models.MatchAny(any=index_tags)))
    if country:
        must.append(models.FieldCondition(
            key="country", match=models.MatchValue(value=country)))
    if active is not None:
        must.append(models.FieldCondition(
            key="active", match=models.MatchValue(value=bool(active))))
    return models.Filter(must=must) if must else None


def _search_one_store(store, query_dense, query_sparse, flt, top_k):
    c = client(store)
    name = collection_name(store)
    if not c.collection_exists(name):
        return []
    dense_req = models.QueryRequest(
        query=query_dense, using="dense", limit=top_k, filter=flt, with_payload=True)
    sp = None
    if isinstance(query_sparse, dict):
        sp = sparse_query_vector(query_sparse)
    elif query_sparse is not None:
        sp = query_sparse
    if sp is None:
        dense_res = c.query_batch_points(name, requests=[dense_req])
        return [p.payload for p in dense_res[0].points]
    sparse_req = models.QueryRequest(
        query=sp, using="sparse", limit=top_k, filter=flt, with_payload=True)
    dense_res, sparse_res = c.query_batch_points(name, requests=[dense_req, sparse_req])
    return rrf_fuse(dense_res.points, sparse_res.points, k=RRF_K)


def hybrid_search(query_dense, query_sparse, specialties=None, language=None, top_k=50,
                  source_corpus=None, doc_types=None, index_tags=None,
                  country=None, active=None):
    flt = _build_filter(specialties, language, source_corpus, doc_types,
                        index_tags, country, active)
    lists = []
    for store in _stores_for_search(
        source_corpus[0] if isinstance(source_corpus, list) and len(source_corpus) == 1
        else (source_corpus if isinstance(source_corpus, str) else None)
    ):
        try:
            hits = _search_one_store(store, query_dense, query_sparse, flt, top_k)
            if hits:
                lists.append(hits)
        except Exception:
            continue
    if not lists:
        return []
    if len(lists) == 1:
        return lists[0]
    return rrf_merge_lists(lists)


def rrf_merge_lists(lists, k=None):
    k = k if k is not None else RRF_K
    scores, payloads = {}, {}
    for lst in lists:
        for rank, p in enumerate(lst):
            key = f"{p.get('doc_hash', '')}|{p.get('chunk_id', p.get('cko_id', ''))}|{p.get('page', '')}"
            if key == "||":
                key = f"fallback:{id(p)}:{rank}"
            scores[key] = scores.get(key, 0) + 1.0 / (k + rank + 1)
            payloads[key] = p
    ranked = sorted(scores, key=scores.get, reverse=True)
    return [payloads[i] for i in ranked]


def rrf_fuse(a, b, k=None):
    k = k if k is not None else RRF_K
    scores, payloads = {}, {}
    for lst in (a, b):
        for rank, p in enumerate(lst):
            scores[p.id] = scores.get(p.id, 0) + 1.0 / (k + rank + 1)
            payloads[p.id] = p.payload
    ranked = sorted(scores, key=scores.get, reverse=True)
    return [payloads[i] for i in ranked]


def scroll(limit=2000, with_payload=True):
    name = collection_name("main")
    return client("main").scroll(name, limit=limit, with_payload=with_payload)
