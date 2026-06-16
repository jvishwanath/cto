"""
Shared repo-name resolution for tools that take a `repo` argument.
Fuzzy-matches user shorthand (e.g., "mgmtapi") to indexed repo names
(e.g., "acme-api"). Falls back to filesystem listing if Qdrant
inventory is unavailable.
"""

import re
from pathlib import Path

from ...config import REPOS_DIR

_SEG_RE = re.compile(r"[-_./]+")


def _segments(name: str) -> list[str]:
    return [s for s in _SEG_RE.split(name.lower()) if s]

_cache: list[str] | None = None


def invalidate_repo_cache() -> None:
    global _cache
    _cache = None


def available_repos(refresh: bool = False) -> list[str]:
    global _cache
    if _cache is not None and not refresh:
        return _cache

    names: set[str] = set()

    # Prefer the indexed inventory (what's actually searchable).
    try:
        from ...ingest.incremental import list_repos
        names.update(r["repo"] for r in list_repos())
    except Exception:
        pass

    # Union with filesystem (covers repos dropped in but not yet indexed).
    base = Path(REPOS_DIR)
    if base.exists():
        names.update(d.name for d in base.iterdir()
                     if d.is_dir() and not d.name.startswith((".", "_")))

    _cache = sorted(names)
    return _cache


def resolve_repo(repo: str | None) -> tuple[str | None, str | None]:
    """
    Returns (resolved_repo, error_message).
    - None input → (None, None) — no filter.
    - Exact match → (repo, None).
    - Single fuzzy match (substring, case-insensitive) → (match, None).
    - No/ambiguous match → (None, helpful error listing options).
    """
    if not repo:
        return None, None

    repos = available_repos()
    if repo in repos:
        return repo, None

    # Segment-aware fuzzy match: a query token matches a repo only if
    # one of the query's hyphen/underscore segments equals (or
    # prefixes/suffixes) one of the repo's segments. This stops raw
    # substring nonsense like 'llm' ⊂ 'enro·llm·ent'. Min length 3
    # avoids 'is'/'in'/'at' triggering on every repo.
    q_segs = [s for s in _segments(repo) if len(s) >= 3]
    if not q_segs:
        avail = ", ".join(repos) if repos else "(none indexed)"
        return None, f"Repo '{repo}' not found. Available repos: {avail}."

    def _hits(r: str) -> bool:
        r_segs = _segments(r)
        for qs in q_segs:
            for rs in r_segs:
                if qs == rs or rs.startswith(qs) or rs.endswith(qs) \
                        or qs.startswith(rs):
                    return True
        return False

    matches = [r for r in repos if _hits(r)]

    if len(matches) == 1:
        return matches[0], None

    avail = ", ".join(repos) if repos else "(none indexed)"
    if len(matches) > 1:
        return None, (f"Repo '{repo}' is ambiguous — matches: {', '.join(matches)}. "
                      f"Use the exact name. Available: {avail}.")
    return None, (f"Repo '{repo}' not found. Available repos: {avail}.")
