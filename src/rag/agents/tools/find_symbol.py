import json

from langchain_core.tools import tool

from ...retrieval.graph_query import find_symbol as _find_symbol
from ._repo import resolve_repo


@tool
def find_symbol(name: str, repo: str | None = None,
                case_sensitive: bool = False) -> str:
    """Find where a symbol (method, function, or class) is defined.

    Use this for exact-name lookup: 'where is X defined', 'find class Y'.
    Returns all matching definitions across repos with file:line.

    Args:
        name: Symbol name. Case-INsensitive by default — pass
              case_sensitive=True for strict match. Not a search query.
        repo: Optional — exact name of an indexed repository to restrict to.
        case_sensitive: Default False. Set True for exact-case match.
    """
    repo, err = resolve_repo(repo)
    if err:
        return err
    rows = _find_symbol(name, repo=repo, case_sensitive=case_sensitive)
    if not rows:
        return f"No symbol named {name!r} found in the code graph."

    results = [
        {
            "repo": r["repo"],
            "file": f"{r['filepath']}:{r['start_line']}",
            "kind": r["kind"],
            "class": r.get("class_name") or "",
            "fqn": r.get("fqn") or "",
        }
        for r in rows
    ]
    return json.dumps(results, indent=2)
