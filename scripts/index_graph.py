"""
Phase 2.5: Build the code graph (symbols + call edges) in Postgres.
Reuses the same data/repos directory as the vector indexer.
Usage: python scripts/index_graph.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from rag.indexing.code_graph import build_graph_for_repos
from rag.retrieval.graph_query import graph_stats

REPOS_DIR = Path(__file__).parent.parent / "data" / "repos"


def main():
    if not REPOS_DIR.exists():
        print(f"No repos found at {REPOS_DIR}")
        sys.exit(1)

    print("Building code graph...")
    stats = build_graph_for_repos(REPOS_DIR)

    print(f"\nPer-repo:")
    for repo, s in stats["repos"].items():
        print(f"  {repo:<25} symbols={s['symbols']:<6} edges={s['edges']}")

    print(f"\nTotal: {stats['symbols']} symbols, {stats['edges']} edges, "
          f"{stats['resolved']} resolved")

    final = graph_stats()
    print(f"Resolution rate: {final['resolution_rate']*100:.1f}% "
          f"({final['resolved']}/{final['edges']} edges link to indexed symbols)")
    print("Unresolved edges = calls to external libraries (expected).")


if __name__ == "__main__":
    main()
