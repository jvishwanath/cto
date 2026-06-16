import json

from langchain_core.tools import tool

from ...retrieval.graph_query import find_callers as _find_callers
from ._repo import resolve_repo


@tool
def find_callers(name: str, depth: int = 2, repo: str | None = None,
                 case_sensitive: bool = False) -> str:
    """Find what calls a given method/function, transitively.

    Use this for: 'what calls X', 'what breaks if I change X',
    'where is X used', impact analysis. Returns the call chain
    with depth (1 = direct caller, 2 = caller of caller, etc).

    Args:
        name: Method/function name. Matched case-INsensitively by
              default — pass case_sensitive=True for strict match.
        depth: How many levels up the call chain (default 2, max 5).
        repo: Optional repo to restrict the target symbol to.
        case_sensitive: Default False. Set True for exact-case match.
    """
    repo, err = resolve_repo(repo)
    if err:
        return err
    depth = min(max(depth, 1), 5)
    rows = _find_callers(name, depth=depth, repo=repo,
                         case_sensitive=case_sensitive)
    if not rows:
        return f"No callers found for {name!r}. It may be an entry point, unused, or external."

    results = [
        {
            "depth": r["depth"],
            "caller": f"{r.get('class_name') or ''}.{r['name']}".lstrip("."),
            "file": f"{r['repo']}/{r['filepath']}:{r['start_line']}",
        }
        for r in rows
    ]
    return json.dumps(results, indent=2)
