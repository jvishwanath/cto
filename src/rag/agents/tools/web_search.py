"""Public-internet search.

Backends, picked at call time:
  - Tavily (when TAVILY_API_KEY is set): structured, higher quality,
    free tier ~1 k req/mo.
  - DuckDuckGo HTML (keyless fallback): scraped from the lite endpoint,
    lower quality but always available.

The tool returns a JSON list of `{url, title, content}` regardless of
backend so skill / agent code can consume it uniformly.
"""

from __future__ import annotations

import html
import json
import re

import httpx
from langchain_core.tools import tool

from ...config import TAVILY_API_KEY

_DDG_URL = "https://html.duckduckgo.com/html/"
_UA = "Mozilla/5.0 (compatible; cto/0.1; +web_search)"
_TIMEOUT = 10.0

# Crude DDG HTML parse — pairs each result anchor with its snippet.
# The HTML layout is stable enough for skill use; if DDG breaks it,
# users can set TAVILY_API_KEY to switch backends.
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>'
    r'(.*?)</a>.*?'
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip(s: str) -> str:
    return html.unescape(_TAG_RE.sub("", s)).strip()


def _ddg_search(query: str, max_results: int) -> list[dict]:
    with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _UA},
                      follow_redirects=True) as c:
        r = c.post(_DDG_URL, data={"q": query})
    r.raise_for_status()
    out: list[dict] = []
    for m in _RESULT_RE.finditer(r.text):
        url, title, snippet = m.group(1), m.group(2), m.group(3)
        # DDG wraps real URLs in a redirect; pull out the `uddg=` target.
        rd = re.search(r"[?&]uddg=([^&]+)", url)
        if rd:
            from urllib.parse import unquote
            url = unquote(rd.group(1))
        out.append({
            "url": html.unescape(url),
            "title": _strip(title),
            "content": _strip(snippet)[:500],
        })
        if len(out) >= max_results:
            break
    return out


def _tavily_search(query: str, max_results: int) -> list[dict]:
    from tavily import TavilyClient
    resp = TavilyClient(api_key=TAVILY_API_KEY).search(
        query=query, max_results=max_results)
    return [
        {"url": r["url"], "title": r.get("title", ""),
         "content": r.get("content", "")[:500]}
        for r in resp.get("results", [])
    ]


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the public internet.

    Use ONLY when internal code and docs are insufficient — third-party
    library behavior, external API specs, RFCs, advisories, general
    technical concepts not covered in the indexed repos.

    Backend: Tavily when TAVILY_API_KEY is set, else keyless DuckDuckGo
    HTML scrape. Returns JSON list of {url, title, content}.

    Args:
        query: Search query.
        max_results: Number of results to return (default 5).
    """
    try:
        results = (_tavily_search(query, max_results) if TAVILY_API_KEY
                   else _ddg_search(query, max_results))
    except Exception as e:
        backend = "Tavily" if TAVILY_API_KEY else "DDG"
        return f"Error: web search failed ({backend}): {e}"

    if not results:
        return f"No web results for: {query!r}"
    return json.dumps(results, indent=2)
