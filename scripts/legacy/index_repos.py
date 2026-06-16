"""
Phase 0: Naive indexer — clone/read repos, chunk by fixed token window, embed, upsert to Qdrant.
Usage: python scripts/index_repos.py
"""

import os
import sys
from pathlib import Path

import tiktoken
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))
from rag.config import QDRANT_URL
from rag.embed import embed_texts

REPOS_DIR = Path(__file__).parents[2] / "data" / "repos"
CHUNK_SIZE = 500  # tokens
CHUNK_OVERLAP = 50  # tokens
COLLECTION = "code"
VECTOR_DIM = 1024

CODE_EXTENSIONS = {
    ".go", ".py", ".js", ".ts", ".java", ".rs", ".rb",
    ".yaml", ".yml", ".json", ".toml", ".sql", ".sh",
    ".md", ".txt", ".dockerfile",
}

tokenizer = tiktoken.get_encoding("cl100k_base")


def chunk_text(text: str, filepath: str, repo: str) -> list[dict]:
    tokens = tokenizer.encode(text)
    chunks = []
    start = 0
    while start < len(tokens):
        end = start + CHUNK_SIZE
        chunk_tokens = tokens[start:end]
        chunk_text = tokenizer.decode(chunk_tokens)

        lines_before = tokenizer.decode(tokens[:start]).count("\n")
        chunks.append({
            "text": chunk_text,
            "metadata": {
                "repo": repo,
                "filepath": filepath,
                "start_line": lines_before + 1,
                "chunk_type": "naive_window",
            },
        })
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def collect_files(repo_path: Path, repo_name: str) -> list[dict]:
    all_chunks = []
    for fpath in repo_path.rglob("*"):
        if not fpath.is_file():
            continue
        if fpath.suffix.lower() not in CODE_EXTENSIONS:
            continue
        if any(part.startswith(".") for part in fpath.relative_to(repo_path).parts):
            continue
        if "vendor" in fpath.parts or "node_modules" in fpath.parts:
            continue

        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        if not text.strip():
            continue

        rel_path = str(fpath.relative_to(repo_path))
        chunks = chunk_text(text, rel_path, repo_name)
        all_chunks.extend(chunks)

    return all_chunks


def main():
    if not REPOS_DIR.exists():
        print(f"No repos found at {REPOS_DIR}")
        print("Clone repos there first:")
        print("  mkdir -p data/repos")
        print("  git clone <repo-url> data/repos/<repo-name>")
        sys.exit(1)

    repos = [d for d in REPOS_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")]
    if not repos:
        print(f"No repo directories found in {REPOS_DIR}")
        sys.exit(1)

    print(f"Found repos: {[r.name for r in repos]}")

    # Collect all chunks
    all_chunks = []
    for repo_path in repos:
        print(f"Processing {repo_path.name}...")
        chunks = collect_files(repo_path, repo_path.name)
        print(f"  → {len(chunks)} chunks")
        all_chunks.append(chunks)

    flat_chunks = [c for repo_chunks in all_chunks for c in repo_chunks]
    print(f"\nTotal chunks: {len(flat_chunks)}")

    # Setup Qdrant collection
    qdrant = QdrantClient(url=QDRANT_URL)
    if qdrant.collection_exists(COLLECTION):
        qdrant.delete_collection(COLLECTION)
    qdrant.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
    )

    # Embed in batches and upsert
    BATCH_SIZE = 64
    point_id = 0
    for i in range(0, len(flat_chunks), BATCH_SIZE):
        batch = flat_chunks[i : i + BATCH_SIZE]
        texts = [c["text"] for c in batch]
        print(f"Embedding batch {i // BATCH_SIZE + 1}/{(len(flat_chunks) + BATCH_SIZE - 1) // BATCH_SIZE}...")

        vectors = embed_texts(texts)

        points = []
        for chunk, vector in zip(batch, vectors):
            points.append(PointStruct(
                id=point_id,
                vector=vector,
                payload={"text": chunk["text"], **chunk["metadata"]},
            ))
            point_id += 1

        qdrant.upsert(collection_name=COLLECTION, points=points)

    print(f"\nDone! Indexed {point_id} chunks into '{COLLECTION}' collection.")


if __name__ == "__main__":
    main()
