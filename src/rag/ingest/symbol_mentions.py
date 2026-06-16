"""
Detect mentions of indexed code symbols inside doc text.
Loads symbol names from Postgres code_symbols once (cached), then
does fast substring + word-boundary matching per doc.
"""

import re

_cache: dict | None = None
_MIN_LEN = 4  # ignore symbols shorter than this — too many false positives
_MAX_SYMBOLS = 50  # cap per doc to keep payload reasonable

# Common words that happen to be method names — exclude to avoid noise.
_STOPWORDS = frozenset({
    "main", "test", "init", "name", "type", "value", "data", "info",
    "size", "list", "item", "code", "text", "path", "file", "read",
    "open", "close", "start", "stop", "next", "find", "load", "save",
    "build", "create", "delete", "update", "check", "error", "config",
    "string", "object", "result", "request", "response", "service",
    "client", "server", "filter", "handler", "model", "true", "false",
})


def _load_index() -> dict:
    """Build {symbol_name: [{repo, filepath, class_name, kind}, ...]}."""
    global _cache
    if _cache is not None:
        return _cache

    idx: dict[str, list[dict]] = {}
    try:
        from ..indexing.graph_db import get_conn
        with get_conn().cursor() as cur:
            cur.execute("""
                SELECT name, repo, filepath, class_name, kind
                FROM code_symbols
                WHERE kind IN ('method','function','class','interface',
                               'struct','enum','trait','constructor')
            """)
            for r in cur.fetchall():
                name = r["name"]
                if len(name) < _MIN_LEN or name.lower() in _STOPWORDS:
                    continue
                idx.setdefault(name, []).append({
                    "repo": r["repo"], "filepath": r["filepath"],
                    "class_name": r["class_name"], "kind": r["kind"],
                })
    except Exception:
        pass

    _cache = idx
    return idx


def invalidate_symbol_cache() -> None:
    global _cache
    _cache = None


def extract_mentions(text: str) -> list[str]:
    """Return distinct symbol names that appear (word-bounded) in text."""
    idx = _load_index()
    if not idx or not text:
        return []

    found: set[str] = set()
    for name in idx:
        if name in text:  # cheap prefilter
            # Word-boundary check so 'sign' doesn't match inside 'design'.
            if re.search(rf"\b{re.escape(name)}\b", text):
                found.add(name)
                if len(found) >= _MAX_SYMBOLS:
                    break

    # Prefer longer/distinctive names first in the stored list.
    return sorted(found, key=lambda s: (-len(s), s))[:_MAX_SYMBOLS]


def repos_for_mentions(mentions: list[str]) -> list[str]:
    """Which repos define any of these symbols (for relates_to tagging)."""
    idx = _load_index()
    repos: set[str] = set()
    for name in mentions:
        for d in idx.get(name, []):
            repos.add(d["repo"])
    return sorted(repos)


def lookup(symbol: str) -> list[dict]:
    """Where is `symbol` defined (for the docs_mentioning tool's enrichment)."""
    return _load_index().get(symbol, [])
