import json

from langchain_core.tools import tool

from ...retrieval import cascade_search
from ._repo import resolve_repo


@tool
def search(query: str, repo: str | None = None, top_k: int = 8) -> str:
    """General-purpose search across ALL indexed sources.

    Cascades through tiers in order: source code → local docs
    (PDF/markdown under data/docs/) → Confluence wiki. Stops early
    when a tier yields strong matches; otherwise falls through and
    merges. Use this FIRST for almost any question — it picks the
    right source automatically. Reach for search_code/search_docs
    only when you specifically need to restrict to one tier.

    Args:
        query: Natural-language search query.
        repo: Optional repo name (fuzzy). For code, filters to that
              repo; for docs/Confluence, treated as a soft hint
              (matches `relates_to`). Omit to search everywhere.
        top_k: Final result count after cross-tier rerank (default 8).
    """
    repo, err = resolve_repo(repo)
    if err:
        return err
    res = cascade_search(query, scope=repo, top_k=max(1, min(top_k, 15)))

    if not res.chunks:
        return (f"No results in any tier ({', '.join(res.visited)}). "
                f"Try a different phrasing or use grep for exact strings.")

    items = []
    for c in res.chunks:
        if c.get("source") == "confluence":
            loc = (c.get("title") or c["filepath"])
            if c.get("section_path"):
                loc += f" › {c['section_path']}"
        elif c.get("source") in ("pdf", "markdown", "text"):
            loc = c["filepath"] + (f" p.{c['page_number']}"
                                   if c.get("page_number") else "")
        else:
            loc = (f"{c.get('repo','')}/{c.get('filepath','')}"
                   f":{c.get('start_line','')}").strip("/:")
            if c.get("symbol_name"):
                loc += f" ({c['symbol_name']})"
        item = {
            "tier": c.get("tier", "code"),
            "loc": loc,
            "score": round(c.get("rerank_score", 0.0), 3),
            "snippet": c.get("text", "")[:500],
        }
        if c.get("url"):
            item["url"] = c["url"]
        items.append(item)

    return json.dumps({
        "visited_tiers": res.visited,
        "stopped_at": res.stopped_at,
        "results": items,
    }, indent=2)
