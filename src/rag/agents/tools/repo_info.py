import json
from pathlib import Path

from langchain_core.tools import tool

from ...config import REPOS_DIR
from ...chunking.service_metadata import extract_repo_metadata
from ._repo import resolve_repo, available_repos


@tool
def repo_info(repo: str | None = None) -> str:
    """Get service-level metadata for a repository: what services it
    depends on, what endpoints it exposes, its service name and URLs.

    Use for: 'what does X connect to', 'what services does X depend on',
    'what does X expose', 'list dependencies of X'. This is the
    SERVICE-level view; for method-level call relationships use
    find_callers / find_callees instead.

    Args:
        repo: Repository name. If omitted, returns metadata for all
              indexed repos (useful for 'what services do we have').
    """
    if repo:
        repo, err = resolve_repo(repo)
        if err:
            return err
        targets = [repo]
    else:
        targets = available_repos(refresh=True)
        if not targets:
            return "No repositories indexed."

    results = []
    for name in targets:
        path = Path(REPOS_DIR) / name
        if not path.exists():
            results.append({"repo": name, "error": "directory not found"})
            continue
        meta = extract_repo_metadata(path)
        results.append({
            "repo": name,
            "service_name": meta["service_name"],
            "depends_on": meta["depends_on"],
            "service_urls": meta.get("service_urls", {}),
            "exposed_endpoints": meta["exposed_endpoints"],
        })

    return json.dumps(results[0] if len(results) == 1 else results, indent=2)
