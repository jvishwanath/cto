import json
import re

from langchain_core.tools import tool

from ...retrieval import hybrid_search, rerank
from ._repo import resolve_repo

# Each chunk is a full method/class (tree-sitter). Cap large enough that
# most fit completely so the agent doesn't need a follow-up read_file.
MAX_SNIPPET_CHARS = 2000

# Strip the index-time context prefix (// Service: ... | // File: ...) —
# that info is already in the structured fields; keeping it in the
# snippet just wastes the char budget.
_PREFIX_RE = re.compile(r"^(?://[^\n]*\n){1,3}")


def _snippet(text: str) -> tuple[str, bool]:
    body = _PREFIX_RE.sub("", text, count=1)
    if len(body) <= MAX_SNIPPET_CHARS:
        return body, False
    cut = body[:MAX_SNIPPET_CHARS]
    # Cut at a line boundary so we don't slice mid-token.
    nl = cut.rfind("\n")
    if nl > MAX_SNIPPET_CHARS * 0.7:
        cut = cut[:nl]
    remaining = body.count("\n", len(cut))
    return cut + f"\n… ({remaining} more lines — use read_file for full content)", True


@tool
def search_code(query: str, repo: str | None = None, top_k: int = 5) -> str:
    """Search the indexed codebase using hybrid (semantic + keyword) retrieval.

    Returns the FULL method/class body for each result (chunks are
    AST-bounded), so you usually do NOT need read_file afterwards.
    Use read_file only when a result is marked truncated, or when you
    need surrounding context (imports, sibling methods, the whole file).

    Args:
        query: Natural language or keyword search query.
        repo: Optional — exact name of an indexed repository to restrict
              results to. Omit to search across all repos.
        top_k: Number of results to return after reranking (default 5).
    """
    repo, err = resolve_repo(repo)
    if err:
        return err
    candidates = hybrid_search(query, top_k=30, repo=repo)
    ranked = rerank(query, candidates, top_k=top_k)

    results = []
    for c in ranked:
        code, truncated = _snippet(c["text"])
        entry = {
            "file": f"{c['repo']}/{c['filepath']}:{c['start_line']}",
            "symbol": c.get("symbol_name") or "",
            "class": c.get("class_name") or "",
            "layer": c.get("layer") or "",
            "score": round(c.get("rerank_score", 0.0), 3),
            "code": code,
        }
        if truncated:
            entry["truncated"] = True
        results.append(entry)

    if not results:
        return f"No results found for query: {query!r}" + (f" in repo {repo}" if repo else "")

    return json.dumps(results, indent=2)
