"""
Phase 1/3.5: full-rebuild AST indexer (dense + Qdrant native BM25).
Thin CLI over rag.ingest.full_index.rebuild_index — the pipeline lives
in the module so the watcher/connectors and any caller can reuse it.

Usage: python scripts/index_repos_v1.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from rag.ingest.full_index import rebuild_index, COLLECTION


def main() -> int:
    n = rebuild_index(recreate=True, progress=True)
    print(f"\nDone! Indexed {n} chunks into '{COLLECTION}' "
          f"(dense + native BM25).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
