"""Fetch a URL, return its text content.

Use for: external docs, RFCs, package/CVE metadata, gists, raw file
content. Returns text — HTML is stripped to plain text; JSON / text /
code returned as-is. Capped at ~50 KB.

Static GET only — no JS execution, no cookies, no auth. For pages that
require a browser, the LLM should fall back to `host_shell` with curl
or a real browser-automation tool.
"""

from __future__ import annotations

import html
import re

import httpx
from langchain_core.tools import tool

_CAP = 50_000
_UA = "Mozilla/5.0 (compatible; cto/0.1; +web_fetch)"
_TIMEOUT = 10.0

_SCRIPT_RE = re.compile(
    r"<(script|style|noscript|template)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_BLANK_RE = re.compile(r"\n{3,}")


def _html_to_text(body: str) -> str:
    s = _SCRIPT_RE.sub("", body)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s)
    s = _BLANK_RE.sub("\n\n", s)
    return s.strip()


@tool
def web_fetch(url: str) -> str:
    """Fetch a URL and return its text content. HTML is stripped to
    plain text; JSON / text / code returned as-is. Output capped at
    ~50 KB. Static GET only (no JS, no cookies, no auth). 10 s timeout,
    follows redirects. Use for external docs, RFCs, advisories,
    package metadata, gists."""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return f"ERROR: only http(s) URLs supported (got {url!r})"
    try:
        with httpx.Client(follow_redirects=True, timeout=_TIMEOUT,
                          headers={"User-Agent": _UA}) as c:
            r = c.get(url)
    except httpx.HTTPError as e:
        return f"ERROR: {type(e).__name__}: {e}"
    if r.status_code >= 400:
        return f"ERROR: HTTP {r.status_code} for {url}"
    ct = (r.headers.get("content-type") or "").lower()
    body = r.text
    if "html" in ct or body.lstrip()[:1] == "<":
        body = _html_to_text(body)
    if len(body) > _CAP:
        body = body[:_CAP] + (
            f"\n\n[truncated: {len(body) - _CAP} more bytes]")
    return body
