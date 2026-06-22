"""
Incremental indexing: per-repo (full rebuild) and per-file granularity.
Idempotent: delete-by-filter then upsert with stable UUID5 point IDs.
Cross-repo graph consistency via FK CASCADE/SET NULL + global resolve_edges().
"""

import fnmatch
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, SparseVectorParams, SparseIndexParams, Modifier,
    PointStruct, Filter, FieldCondition, MatchValue, FilterSelector,
    TurboQuantization, TurboQuantQuantizationConfig, TurboQuantBitSize
)

from ..config import QDRANT_URL, REPOS_DIR, VECTOR_DIM
from ..embed import embed_texts
from ..chunking import chunk_file
from ..chunking.service_metadata import (
    extract_repo_metadata, extract_file_metadata, build_context_prefix,
    metadata_source_patterns,
)
from ..retrieval.hybrid import sparse_doc
from ..indexing.graph_db import setup_schema
from ..indexing.code_graph import (
    index_repo_graph, index_files_graph, delete_graph_for, resolve_edges,
)

log = logging.getLogger(__name__)

CODE_COLLECTION = "code"
POINT_NS = uuid.UUID("3f2504e0-4f89-41d3-9a0c-0305e82c3301")
FULL_REBUILD_THRESHOLD = 0.20

CODE_EXTENSIONS = {
    ".java", ".py", ".go", ".js", ".ts", ".tsx", ".jsx",
    ".rs", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".h", ".c",
    ".tf", ".tfvars", ".hcl",
    ".yaml", ".yml", ".json", ".toml", ".sql", ".sh",
    ".md", ".txt", ".dockerfile", ".gradle", ".xml", ".properties",
}
_SKIP_DIRS = {
    "vendor", "node_modules", "build", "target", "dist", "__pycache__",
    ".terraform",  # provider mirrors + vendored modules
    # When this project is symlinked into its own data/repos/, do not
    # descend into data/ (qdrant/postgres/phoenix storage → feedback loop).
    "data", ".claude", ".venv", "venv",
    "Pods", "pods", ".gradle", ".dart_tool", ".pub-cache"
}

_qdrant: QdrantClient | None = None


def _client() -> QdrantClient:
    global _qdrant
    if _qdrant is None:
        _qdrant = QdrantClient(url=QDRANT_URL)
    return _qdrant


@dataclass
class IndexResult:
    repo: str
    mode: str               # "full" | "file" | "delete"
    files_processed: int = 0
    chunks: int = 0
    symbols: int = 0
    edges: int = 0
    escalated: bool = False
    escalation_reason: str = ""
    deleted_files: list[str] = field(default_factory=list)


# ── Qdrant helpers ───────────────────────────────────────────────────

def ensure_code_collection(recreate: bool = False) -> None:
    c = _client()
    if recreate and c.collection_exists(CODE_COLLECTION):
        c.delete_collection(CODE_COLLECTION)
    if not c.collection_exists(CODE_COLLECTION):
        c.create_collection(
            collection_name=CODE_COLLECTION,
            vectors_config={"dense": VectorParams(size=VECTOR_DIM, distance=Distance.COSINE)},
            sparse_vectors_config={"sparse": SparseVectorParams(
                index=SparseIndexParams(on_disk=False), modifier=Modifier.IDF,
            )},
            quantization_config=TurboQuantization(
                turbo=TurboQuantQuantizationConfig(
                    bits=TurboQuantBitSize.BITS4,
                    always_ram=True
                )
            ),
        )


def _delete_qdrant(repo: str, filepath: str | None = None) -> None:
    must = [FieldCondition(key="repo", match=MatchValue(value=repo))]
    if filepath:
        must.append(FieldCondition(key="filepath", match=MatchValue(value=filepath)))
    _client().delete(
        collection_name=CODE_COLLECTION,
        points_selector=FilterSelector(filter=Filter(must=must)),
    )


def _chunk_id(repo: str, filepath: str, idx: int) -> str:
    return str(uuid.uuid5(POINT_NS, f"{repo}|{filepath}|{idx}"))


# ── File walking / filtering ─────────────────────────────────────────

def _is_indexable(repo_path: Path, fpath: Path) -> bool:
    if not fpath.is_file():
        return False
    if fpath.suffix.lower() not in CODE_EXTENSIONS:
        return False
    rel_parts = fpath.relative_to(repo_path).parts
    if any(p.startswith(".") for p in rel_parts):
        return False
    if any(p in _SKIP_DIRS for p in rel_parts):
        return False
    return True


def _list_indexable(repo_path: Path) -> list[str]:
    return sorted(
        str(f.relative_to(repo_path))
        for f in repo_path.rglob("*")
        if _is_indexable(repo_path, f)
    )


# ── Chunking + upsert ────────────────────────────────────────────────

def _chunk_and_enrich(repo_path: Path, repo_name: str, files: list[str],
                      repo_meta: dict) -> list[dict]:
    chunks: list[dict] = []
    for rel in files:
        fpath = repo_path / rel
        if not fpath.exists():
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not text.strip():
            continue

        file_chunks = chunk_file(rel, text, repo_name)
        file_meta = extract_file_metadata(rel, text, repo_meta)
        for i, ch in enumerate(file_chunks):
            ch["metadata"]["service_name"] = repo_meta["service_name"]
            ch["metadata"]["depends_on"] = repo_meta["depends_on"]
            ch["metadata"]["layer"] = file_meta["layer"]
            if file_meta["calls_services"]:
                ch["metadata"]["calls_services"] = file_meta["calls_services"]
            ch["metadata"]["chunk_idx"] = i
            prefix = build_context_prefix(ch["metadata"], repo_meta, file_meta)
            ch["text"] = f"{prefix}\n{ch['text']}"
        chunks.extend(file_chunks)
    return chunks


def _upsert(chunks: list[dict], batch_size: int = 64) -> int:
    n = 0
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        dense = embed_texts([c["text"] for c in batch])
        points = []
        for ch, dv in zip(batch, dense):
            m = ch["metadata"]
            points.append(PointStruct(
                id=_chunk_id(m["repo"], m["filepath"], m.get("chunk_idx", 0)),
                vector={"dense": dv, "sparse": sparse_doc(ch["text"])},
                payload={"text": ch["text"], **m},
            ))
        _client().upsert(collection_name=CODE_COLLECTION, points=points)
        n += len(points)
    return n


# ── Escalation logic ─────────────────────────────────────────────────

def _should_escalate(repo_path: Path, files: list[str], deleted: set[str],
                     total_files: int) -> tuple[bool, str]:
    touched = set(files) | deleted
    if not touched:
        return False, ""

    # Metadata-source files changed → full rebuild (chunk metadata is repo-wide)
    patterns = metadata_source_patterns(repo_path)
    for f in touched:
        for pat in patterns:
            if fnmatch.fnmatch(f, pat) or fnmatch.fnmatch(Path(f).name, pat):
                return True, f"metadata source changed: {f}"

    # Mass change → full rebuild is faster than N file-level deletes
    if total_files > 0 and len(touched) / total_files > FULL_REBUILD_THRESHOLD:
        return True, f"{len(touched)}/{total_files} files (>{FULL_REBUILD_THRESHOLD:.0%}) changed"

    return False, ""


# ── Public API ───────────────────────────────────────────────────────

def index_repo(name: str, files: list[str] | None = None,
               deleted: set[str] | None = None) -> IndexResult:
    """
    Index a repo into Qdrant (chunks) + Postgres (call graph).

    files=None  → full rebuild: delete everything for this repo, walk all files.
    files=[...] → file-level: delete + re-index only these paths. Auto-escalates
                  to full rebuild if metadata-source files changed or >20% touched.
    deleted     → file paths to remove (no re-index). Applied before files.
    """
    deleted = set(deleted or [])
    repo_path = Path(REPOS_DIR) / name

    ensure_code_collection()
    setup_schema()

    if not repo_path.exists():
        # Repo directory removed → delete everything for it.
        return delete_repo(name)

    repo_meta = extract_repo_metadata(repo_path)
    all_files = _list_indexable(repo_path)

    # Decide mode
    if files is None:
        mode = "full"
        escalated, reason = False, ""
    else:
        files = [f for f in files
                 if _is_indexable(repo_path, repo_path / f) or f in deleted]
        escalated, reason = _should_escalate(repo_path, files, deleted, len(all_files))
        mode = "full" if escalated else "file"

    result = IndexResult(repo=name, mode=mode, escalated=escalated,
                         escalation_reason=reason,
                         deleted_files=sorted(deleted))

    if mode == "full":
        log.info("index_repo[%s] FULL%s", name, f" (escalated: {reason})" if escalated else "")
        _delete_qdrant(name)
        delete_graph_for(name)

        chunks = _chunk_and_enrich(repo_path, name, all_files, repo_meta)
        result.chunks = _upsert(chunks)
        result.files_processed = len(all_files)

        n_sym, n_edge = index_repo_graph(repo_path, name, set())
        result.symbols, result.edges = n_sym, n_edge

    else:
        log.info("index_repo[%s] FILE-LEVEL: %d changed, %d deleted",
                 name, len(files), len(deleted))

        # Deletions first (file removed/renamed-from)
        for rel in deleted:
            _delete_qdrant(name, rel)
            delete_graph_for(name, rel)

        # Per-file delete + re-index
        for rel in files:
            _delete_qdrant(name, rel)
            delete_graph_for(name, rel)

        chunks = _chunk_and_enrich(repo_path, name, files, repo_meta)
        result.chunks = _upsert(chunks)
        result.files_processed = len(files)

        n_sym, n_edge = index_files_graph(repo_path, name, files)
        result.symbols, result.edges = n_sym, n_edge

    # Re-link orphaned cross-repo/cross-file edges (global, idempotent).
    resolve_edges()
    return result


def delete_repo(name: str) -> IndexResult:
    log.info("delete_repo[%s]", name)
    ensure_code_collection()
    setup_schema()
    _delete_qdrant(name)
    delete_graph_for(name)
    resolve_edges()
    return IndexResult(repo=name, mode="delete")


def list_repos() -> list[dict]:
    """Distinct indexed repos with chunk counts (for UI sidebar)."""
    c = _client()
    if not c.collection_exists(CODE_COLLECTION):
        return []
    counts: dict[str, int] = {}
    offset = None
    while True:
        pts, offset = c.scroll(CODE_COLLECTION, limit=512, offset=offset,
                               with_payload=["repo"], with_vectors=False)
        for p in pts:
            r = p.payload.get("repo", "?")
            counts[r] = counts.get(r, 0) + 1
        if offset is None:
            break
    return [{"repo": k, "chunks": v} for k, v in sorted(counts.items())]
