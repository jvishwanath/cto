"""
Docs ingestion: PDF/Markdown/text → chunks → 'docs' Qdrant collection.
Idempotent per file: delete-by-filter on filepath, then upsert.
"""

import os
import uuid
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, SparseVectorParams, SparseIndexParams, Modifier,
    PointStruct, Filter, FieldCondition, MatchValue, FilterSelector,
    TurboQuantization, TurboQuantQuantizationConfig, TurboQuantBitSize
)

DOCS_COLLECTION = "docs"

from ..config import QDRANT_URL, REPOS_DIR, VECTOR_DIM
from ..embed import embed_texts
from ..chunking.markdown import chunk_markdown_file
from ..retrieval.hybrid import sparse_doc
from .pdf import pdf_to_markdown, page_for_line
from .symbol_mentions import extract_mentions, repos_for_mentions

DOCS_DIR = Path(os.environ.get(
    "DOCS_DIR", str(Path(__file__).parents[3] / "data" / "docs")
))
_POINT_NS = uuid.UUID("9c1b6f5a-3e3f-4f9b-9c8d-1a2b3c4d5e6f")
_DOC_EXTS = {".pdf", ".md", ".markdown", ".txt"}

_qdrant: QdrantClient | None = None


def _client() -> QdrantClient:
    global _qdrant
    if _qdrant is None:
        _qdrant = QdrantClient(url=QDRANT_URL)
    return _qdrant


def ensure_docs_collection(recreate: bool = False) -> None:
    c = _client()
    if recreate and c.collection_exists(DOCS_COLLECTION):
        c.delete_collection(DOCS_COLLECTION)
    if not c.collection_exists(DOCS_COLLECTION):
        c.create_collection(
            collection_name=DOCS_COLLECTION,
            vectors_config={"dense": VectorParams(size=VECTOR_DIM, distance=Distance.COSINE)},
            sparse_vectors_config={"sparse": SparseVectorParams(
                index=SparseIndexParams(on_disk=False),
                modifier=Modifier.IDF,
            )},
            quantization_config=TurboQuantization(
                turbo=TurboQuantQuantizationConfig(
                    bits=TurboQuantBitSize.BITS4,
                    always_ram=True
                )
            ),
        )


def _extract(path: Path) -> tuple[str, dict[int, int], str]:
    """Returns (markdown_text, line→page map, source_type)."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        md, line_pages = pdf_to_markdown(path)
        return md, line_pages, "pdf"
    text = path.read_text(encoding="utf-8", errors="ignore")
    return text, {}, "markdown" if ext in (".md", ".markdown") else "text"


def _repo_from_path(rel: str) -> str | None:
    """Convention: data/docs/<repo>/... → tag chunks with that repo if it
    exists in data/repos/. Otherwise None (un-scoped doc)."""
    parts = Path(rel).parts
    if len(parts) < 2:
        return None
    candidate = parts[0]
    if (Path(REPOS_DIR) / candidate).is_dir():
        return candidate
    return None


def index_doc(path: str | Path) -> int:
    """
    Extract → chunk → embed → upsert into 'docs'. Idempotent: existing
    chunks for this filepath are deleted first. Returns chunk count.
    """
    path = Path(path)
    if not path.exists() or path.suffix.lower() not in _DOC_EXTS:
        return 0

    rel = str(path.relative_to(DOCS_DIR)) if str(path).startswith(str(DOCS_DIR)) else path.name

    ensure_docs_collection()
    delete_doc(rel)

    md, line_pages, source_type = _extract(path)
    if not md.strip():
        return 0

    # Doc-level code linkage: detect repo by path convention, and
    # extract mentions of indexed code symbols from the full text.
    repo_tag = _repo_from_path(rel)
    doc_mentions = extract_mentions(md)
    relates_to = sorted(set(
        ([repo_tag] if repo_tag else []) + repos_for_mentions(doc_mentions)
    ))

    chunks = chunk_markdown_file(rel, md, repo="docs")
    for i, ch in enumerate(chunks):
        ch["metadata"]["source"] = source_type
        ch["metadata"]["filepath"] = rel
        ch["metadata"]["chunk_idx"] = i
        if repo_tag:
            ch["metadata"]["repo"] = repo_tag
        if relates_to:
            ch["metadata"]["relates_to"] = relates_to
        # Per-chunk symbol mentions (subset of doc-level, scoped to this section).
        chunk_mentions = [m for m in doc_mentions if m in ch["text"]]
        if chunk_mentions:
            ch["metadata"]["mentions_symbols"] = chunk_mentions
        if source_type == "pdf":
            ch["metadata"]["page_number"] = page_for_line(
                line_pages, ch["metadata"].get("start_line", 1)
            )

    n = 0
    BATCH = 64
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i:i + BATCH]
        dense = embed_texts([c["text"] for c in batch])
        points = []
        for ch, dv in zip(batch, dense):
            pid = str(uuid.uuid5(_POINT_NS, f"docs|{rel}|{ch['metadata']['chunk_idx']}"))
            points.append(PointStruct(
                id=pid,
                vector={"dense": dv, "sparse": sparse_doc(ch["text"])},
                payload={"text": ch["text"], **ch["metadata"]},
            ))
        _client().upsert(collection_name=DOCS_COLLECTION, points=points)
        n += len(points)

    return n


def delete_doc(rel_path: str) -> None:
    if not _client().collection_exists(DOCS_COLLECTION):
        return
    _client().delete(
        collection_name=DOCS_COLLECTION,
        points_selector=FilterSelector(filter=Filter(must=[
            FieldCondition(key="filepath", match=MatchValue(value=rel_path)),
        ])),
    )


def index_docs_dir(directory: str | Path | None = None) -> dict:
    """Index every supported file under data/docs/."""
    base = Path(directory) if directory else DOCS_DIR
    base.mkdir(parents=True, exist_ok=True)
    ensure_docs_collection()

    stats = {}
    for fpath in base.rglob("*"):
        if fpath.is_file() and fpath.suffix.lower() in _DOC_EXTS:
            n = index_doc(fpath)
            stats[str(fpath.relative_to(base))] = n
    return stats


def list_docs() -> list[dict]:
    """Distinct indexed docs with chunk counts (for UI sidebar)."""
    c = _client()
    if not c.collection_exists(DOCS_COLLECTION):
        return []
    counts: dict[str, dict] = {}
    offset = None
    while True:
        pts, offset = c.scroll(
            DOCS_COLLECTION, limit=256, offset=offset,
            with_payload=["filepath", "source", "page_number"], with_vectors=False,
        )
        for p in pts:
            fp = p.payload["filepath"]
            d = counts.setdefault(fp, {"filepath": fp, "source": p.payload.get("source"), "chunks": 0, "pages": 0})
            d["chunks"] += 1
            if p.payload.get("page_number"):
                d["pages"] = max(d["pages"], p.payload["page_number"])
        if offset is None:
            break
    return sorted(counts.values(), key=lambda d: d["filepath"])
