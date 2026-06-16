import json

from langchain_core.tools import tool

from ...retrieval import hybrid_search, rerank
from ...ingest.docs import DOCS_COLLECTION
from ._repo import resolve_repo


@tool
def search_docs(query: str, repo: str | None = None, top_k: int = 6) -> str:
    """Search indexed documentation (PDFs, markdown, design docs, runbooks).

    Use this for: architecture/design questions, runbook procedures,
    'what does the spec say about X', anything answered by prose docs
    rather than source code. For code questions use search_code instead.

    Args:
        query: Natural language search query.
        repo: Optional — restrict to docs tagged for one repo (docs placed
              under data/docs/<repo>/ or that mention that repo's symbols).
        top_k: Number of results after reranking (default 6).
    """
    repo, err = resolve_repo(repo)
    if err:
        return err
    candidates = hybrid_search(query, top_k=30, collection=DOCS_COLLECTION, repo=repo)
    if not candidates:
        return "No documents indexed yet (data/docs/ is empty or 'docs' collection does not exist)."

    ranked = rerank(query, candidates, top_k=top_k)

    results = []
    for c in ranked:
        loc = c["filepath"]
        if c.get("page_number"):
            loc += f" p.{c['page_number']}"
        elif c.get("start_line"):
            loc += f":{c['start_line']}"
        item = {
            "doc": loc,
            "source": c.get("source") or "",
            "section": c.get("section_path") or "",
            "repo": c.get("repo") or None,
            "mentions_symbols": c.get("mentions_symbols") or [],
            "score": round(c.get("rerank_score", 0.0), 3),
            "snippet": c["text"][:600],
        }
        if c.get("url"):
            item["url"] = c["url"]
            item["title"] = c.get("title") or c.get("breadcrumb") or loc
        results.append(item)

    return json.dumps(results, indent=2)
