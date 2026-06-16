# scripts/legacy/

Phase-0 baseline scripts — naive fixed-window chunking, dense-only
retrieval, no rerank. Kept **only** for the eval comparison ("every
later phase must beat the Phase-0 number"); not used in normal
operation.

| Script | Superseded by |
|---|---|
| `index_repos.py` | `scripts/index_repos_v1.py` → `rag.ingest.full_index` (AST chunking + hybrid + native BM25) |
| `query.py` | the agent (`make ask`), or `scripts/query_v1.py` (hybrid + rerank) |

`make index` / `make query` still point here for baseline runs.
