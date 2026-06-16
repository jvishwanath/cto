"""
Hybrid retrieval: dense (embedding) + sparse (Qdrant native BM25) fused with RRF.

Sparse vectors are computed server-side by Qdrant using its built-in
tokenizer + IDF weighting (`Modifier.IDF`). No client-side vocabulary —
the index updates IDF stats incrementally on insert/delete, which is
what makes file-level incremental indexing safe.
"""

from qdrant_client import QdrantClient, models
from qdrant_client.models import (
    FusionQuery,
    Fusion,
    Prefetch,
    Filter,
    FieldCondition,
    MatchValue,
    MatchAny,
)

from ..config import QDRANT_URL
from ..embed import embed_query

COLLECTION = "code"
SPARSE_MODEL = "Qdrant/bm25"

_qdrant: QdrantClient | None = None


def _client() -> QdrantClient:
    global _qdrant
    if _qdrant is None:
        _qdrant = QdrantClient(url=QDRANT_URL)
    return _qdrant


def sparse_doc(text: str) -> models.Document:
    """Wrap text for server-side BM25 tokenization (used at index and query time)."""
    return models.Document(text=text, model=SPARSE_MODEL)


def hybrid_search(
    query: str,
    top_k: int = 30,
    collection: str = COLLECTION,
    repo: str | list[str] | None = None,
    layer: str | None = None,
    source: str | None = None,
) -> list[dict]:
    """
    Dense + sparse (BM25) search with RRF fusion via Qdrant prefetch.
    Optional payload filters: repo, layer, source.
    """
    if not _client().collection_exists(collection):
        return []

    dense_vector = embed_query(query)

    must = []
    should = []
    if repo:
        repos = repo if isinstance(repo, list) else [repo]
        match = MatchAny(any=repos) if len(repos) > 1 else MatchValue(value=repos[0])
        if collection == COLLECTION:
            must.append(FieldCondition(key="repo", match=match))
        else:
            # Docs collection: local docs use `repo`, Confluence uses
            # `relates_to` (symbol-mention linkage). Match either.
            should = [
                FieldCondition(key="repo", match=match),
                FieldCondition(key="relates_to", match=MatchAny(any=repos)),
            ]
    if layer:
        must.append(FieldCondition(key="layer", match=MatchValue(value=layer)))
    if source:
        must.append(FieldCondition(key="source", match=MatchValue(value=source)))
    qfilter = (Filter(must=must or None, should=should or None)
               if (must or should) else None)

    results = _client().query_points(
        collection_name=collection,
        prefetch=[
            Prefetch(query=dense_vector, using="dense", limit=top_k, filter=qfilter),
            Prefetch(query=sparse_doc(query), using="sparse", limit=top_k, filter=qfilter),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        limit=top_k,
        with_payload=True,
    )

    return [
        {
            "text": p.payload.get("text", ""),
            "repo": p.payload.get("repo", ""),
            "filepath": p.payload.get("filepath", ""),
            "start_line": p.payload.get("start_line", 0),
            "symbol_name": p.payload.get("symbol_name", ""),
            "class_name": p.payload.get("class_name", ""),
            "chunk_type": p.payload.get("chunk_type", ""),
            "service_name": p.payload.get("service_name", ""),
            "layer": p.payload.get("layer", ""),
            "source": p.payload.get("source", ""),
            "page_number": p.payload.get("page_number"),
            "section_path": p.payload.get("section_path", ""),
            "mentions_symbols": p.payload.get("mentions_symbols"),
            "relates_to": p.payload.get("relates_to"),
            "score": p.score,
        }
        for p in results.points
    ]
