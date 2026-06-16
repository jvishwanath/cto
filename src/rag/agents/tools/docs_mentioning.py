import json

from langchain_core.tools import tool
from qdrant_client.models import Filter, FieldCondition, MatchAny

from ...ingest.docs import DOCS_COLLECTION, _client
from ...ingest.symbol_mentions import lookup


@tool
def docs_mentioning(symbol: str) -> str:
    """Find documentation (PDFs, design docs, runbooks) that explicitly
    mentions a specific code symbol by name.

    Use for: 'is X documented anywhere', 'which design doc covers
    method/class X', 'find the runbook for X'. This is an EXACT-name
    lookup against doc payloads — not a semantic search. Pair with
    find_symbol to confirm where the symbol is defined.

    Args:
        symbol: Exact method/function/class name (e.g., 'validateCSR').
    """
    c = _client()
    if not c.collection_exists(DOCS_COLLECTION):
        return "No documents indexed yet."

    pts, _ = c.scroll(
        DOCS_COLLECTION,
        scroll_filter=Filter(must=[FieldCondition(
            key="mentions_symbols", match=MatchAny(any=[symbol]),
        )]),
        limit=100,
        with_payload=["filepath", "source", "section_path", "page_number",
                      "repo", "relates_to", "mentions_symbols"],
        with_vectors=False,
    )

    if not pts:
        defined = lookup(symbol)
        hint = ""
        if defined:
            locs = ", ".join(f"{d['repo']}/{d['filepath']}" for d in defined[:3])
            hint = f" (Symbol IS defined in code at: {locs})"
        return f"No docs mention '{symbol}'.{hint}"

    # Group by document, collect sections/pages where it appears.
    by_doc: dict[str, dict] = {}
    for p in pts:
        fp = p.payload["filepath"]
        d = by_doc.setdefault(fp, {
            "doc": fp,
            "source": p.payload.get("source"),
            "repo": p.payload.get("repo"),
            "relates_to": p.payload.get("relates_to", []),
            "occurrences": [],
        })
        loc = {}
        if p.payload.get("page_number"):
            loc["page"] = p.payload["page_number"]
        if p.payload.get("section_path"):
            loc["section"] = p.payload["section_path"]
        d["occurrences"].append(loc)

    defined = lookup(symbol)
    return json.dumps({
        "symbol": symbol,
        "defined_in": [
            f"{d['repo']}/{d['filepath']} ({d['kind']}"
            + (f" in {d['class_name']}" if d.get("class_name") else "") + ")"
            for d in defined[:5]
        ],
        "mentioned_in_docs": list(by_doc.values()),
    }, indent=2)
