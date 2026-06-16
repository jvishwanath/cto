import json

from langchain_core.tools import tool

from ...retrieval.graph_query import find_callees as _find_callees
from ._repo import resolve_repo


@tool
def find_callees(name: str, depth: int = 2, repo: str | None = None,
                 case_sensitive: bool = False) -> str:
    """Find what a given method/function calls, transitively.

    Use this for: 'what does X call', 'what services/APIs does X use',
    'trace the flow starting from X'. Returns the callee chain with
    depth. Callees marked kind='external' are not in the indexed repos
    (e.g., third-party libraries).

    Args:
        name: Method/function name. Matched case-INsensitively by
              default — pass case_sensitive=True for strict match.
        depth: How many levels down the call chain (default 2, max 5).
        repo: Optional repo to restrict the source symbol to.
        case_sensitive: Default False. Set True for exact-case match.
    """
    repo, err = resolve_repo(repo)
    if err:
        return err
    depth = min(max(depth, 1), 5)
    rows = _find_callees(name, depth=depth, repo=repo,
                         case_sensitive=case_sensitive)
    if not rows:
        return f"No callees found for {name!r}. It may be a leaf function or not in the graph."

    results = [
        {
            "depth": r["depth"],
            "callee": f"{r.get('class_name') or r.get('via_class') or ''}.{r['name']}".lstrip("."),
            "kind": r["kind"],
            "file": f"{r['repo']}/{r['filepath']}:{r['start_line']}" if r.get("repo") else "(external)",
        }
        for r in rows
    ]
    return json.dumps(results, indent=2)
