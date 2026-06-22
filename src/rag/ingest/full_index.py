"""
Full-rebuild indexing pipeline (Phase 1/3.5): walk every repo under
REPOS_DIR, AST-chunk, enrich with service metadata, embed (dense +
Qdrant native BM25 sparse), upsert into the 'code' collection with
stable UUID5 point IDs.

This is the from-scratch counterpart to ingest/incremental.index_repo
(which does file-level deltas). Both should ultimately share chunk →
embed → upsert; for now this module owns the full-walk path and the
CLI (scripts/index_repos_v1.py) is a thin wrapper.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, SparseVectorParams, SparseIndexParams,
    Modifier, PointStruct,
    TurboQuantization, TurboQuantQuantizationConfig, TurboQuantBitSize
)

from ..config import QDRANT_URL, REPOS_DIR, VECTOR_DIM
from ..embed import embed_texts
from ..chunking import chunk_file
from ..chunking.service_metadata import (
    extract_repo_metadata, extract_file_metadata, build_context_prefix,
)
from ..retrieval.hybrid import sparse_doc

COLLECTION = "code"
POINT_NS = uuid.UUID("3f2504e0-4f89-41d3-9a0c-0305e82c3301")

CODE_EXTENSIONS = {
    ".java", ".py", ".go", ".js", ".ts", ".tsx", ".jsx",
    ".rs", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".h", ".c",
    ".tf", ".tfvars", ".hcl",
    ".yaml", ".yml", ".json", ".toml", ".sql", ".sh",
    ".md", ".txt", ".dockerfile", ".gradle", ".xml", ".properties",
}
# .terraform/ holds provider mirrors + modules — vendored, skip.
_SKIP_PARTS = {
    "vendor", "node_modules", "build", "target", "dist", ".terraform",
    ".venv", "venv", "data", ".git", ".pytest_cache", "__pycache__",
    "Pods", "pods", ".gradle", ".dart_tool", ".pub-cache"
}


def chunk_id(repo: str, filepath: str, idx: int) -> str:
    """Stable UUID5 so re-indexing the same chunk replaces it."""
    return str(uuid.uuid5(POINT_NS, f"{repo}|{filepath}|{idx}"))


def collect_repo_chunks(repo_path: Path) -> list[dict]:
    """Chunk + enrich every indexable file in one repo."""
    repo_name = repo_path.name
    repo_meta = extract_repo_metadata(repo_path)
    out: list[dict] = []

    for fpath in repo_path.rglob("*"):
        if not fpath.is_file() or fpath.suffix.lower() not in CODE_EXTENSIONS:
            continue
        rel_parts = fpath.relative_to(repo_path).parts
        if any(p.startswith(".") for p in rel_parts):
            continue
        if any(p in _SKIP_PARTS for p in rel_parts):
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not text.strip():
            continue

        rel_path = str(fpath.relative_to(repo_path))
        chunks = chunk_file(rel_path, text, repo_name)
        file_meta = extract_file_metadata(rel_path, text, repo_meta)
        for i, chunk in enumerate(chunks):
            m = chunk["metadata"]
            m["service_name"] = repo_meta["service_name"]
            m["depends_on"] = repo_meta["depends_on"]
            m["layer"] = file_meta["layer"]
            if file_meta["calls_services"]:
                m["calls_services"] = file_meta["calls_services"]
            m["chunk_idx"] = i
            prefix = build_context_prefix(m, repo_meta, file_meta)
            chunk["text"] = f"{prefix}\n{chunk['text']}"
        out.extend(chunks)
    return out


def collect_all_chunks(repos_dir: Path | None = None) -> list[dict]:
    """Walk every repo under repos_dir (default REPOS_DIR)."""
    repos_dir = Path(repos_dir or REPOS_DIR)
    repos = [d for d in repos_dir.iterdir()
             if d.is_dir() and not d.name.startswith(".")]
    all_chunks: list[dict] = []
    for repo_path in repos:
        chunks = collect_repo_chunks(repo_path)
        all_chunks.extend(chunks)
    return all_chunks


def ensure_collection(qdrant: QdrantClient, name: str = COLLECTION,
                      recreate: bool = False) -> None:
    """Create the 'code' collection (dense + native-BM25 sparse)."""
    if recreate and qdrant.collection_exists(name):
        qdrant.delete_collection(name)
    if not qdrant.collection_exists(name):
        qdrant.create_collection(
            collection_name=name,
            vectors_config={
                "dense": VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
            },
            sparse_vectors_config={
                "sparse": SparseVectorParams(
                    index=SparseIndexParams(on_disk=False),
                    modifier=Modifier.IDF,
                ),
            },
            quantization_config=TurboQuantization(
                turbo=TurboQuantQuantizationConfig(
                    bits=TurboQuantBitSize.BITS4,
                    always_ram=True
                )
            ),
        )


def upsert_chunks(qdrant: QdrantClient, chunks: list[dict],
                  collection: str = COLLECTION, batch_size: int | None = None,
                  progress: bool = False) -> int:
    """Embed and upsert chunks. Sparse via Qdrant server-side BM25."""
    import os
    if batch_size is None:
        batch_size = int(os.environ.get("EMBED_BATCH_SIZE", "8"))
    n = 0
    total = (len(chunks) + batch_size - 1) // batch_size
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        if progress:
            print(f"Embedding batch {i // batch_size + 1}/{total}…")
        dense = embed_texts([c["text"] for c in batch])
        points = []
        for chunk, dv in zip(batch, dense):
            m = chunk["metadata"]
            points.append(PointStruct(
                id=chunk_id(m["repo"], m["filepath"], m.get("chunk_idx", 0)),
                vector={"dense": dv, "sparse": sparse_doc(chunk["text"])},
                payload={"text": chunk["text"], **m},
            ))
        qdrant.upsert(collection_name=collection, points=points)
        n += len(points)
    return n


def rebuild_index(repos_dir: Path | None = None, *,
                  recreate: bool = True, progress: bool = True) -> int:
    """Full pipeline: collect → (re)create collection → upsert.
    Returns chunk count. Used by scripts/index_repos_v1.py."""
    chunks = collect_all_chunks(repos_dir)
    qdrant = QdrantClient(url=QDRANT_URL)
    ensure_collection(qdrant, COLLECTION, recreate=recreate)
    return upsert_chunks(qdrant, chunks, progress=progress)
