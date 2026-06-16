"""
Web-tool tests — no live network. Mocks httpx.Client.

Covers:
  * web_fetch: URL validation, HTML → text strip, JSON pass-through,
    HTTP error, transport error, cap/truncation.
  * web_search: DDG fallback when TAVILY_API_KEY unset (parsed result
    shape, uddg redirect unwrap, empty results); Tavily branch when
    key present; backend error returns string.
  * Superdev wiring: both tools resolve via TOOLS_BY_NAME and appear
    in build_host_tools toolset.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# embed.py builds an OpenAI client at import time. Stub creds so
# `import rag.agents.tools.*` works in CI / local without secrets.
os.environ.setdefault("LITELLM_API_KEY", "sk-test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

import conftest  # noqa: F401

from smoke_test import check, section, results  # noqa: E402


def _resp(status: int, text: str = "", content_type: str = "text/html"):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.headers = {"content-type": content_type}
    r.raise_for_status = MagicMock(
        side_effect=None if status < 400 else Exception(f"HTTP {status}"))
    return r


def _client_ctx(resp):
    c = MagicMock()
    c.get.return_value = resp
    c.post.return_value = resp
    cm = MagicMock()
    cm.__enter__.return_value = c
    cm.__exit__.return_value = False
    return cm


# ── web_fetch ────────────────────────────────────────────────────────

def test_web_fetch():
    section("web_fetch")
    from rag.agents.tools.web_fetch import web_fetch
    import httpx as _httpx

    @check("rejects non-http URL")
    def _():
        out = web_fetch.invoke({"url": "file:///etc/passwd"})
        assert out.startswith("ERROR"), out

    html_body = (
        "<html><head><style>x{}</style><script>evil()</script>"
        "<title>T</title></head><body><p>Hello <b>world</b>!</p>"
        "<p>&amp; more</p></body></html>")

    @check("strips HTML to plain text")
    def _():
        with patch("rag.agents.tools.web_fetch.httpx.Client",
                   return_value=_client_ctx(_resp(200, html_body))):
            out = web_fetch.invoke({"url": "https://example.com"})
        assert "evil()" not in out, out
        assert "x{}" not in out, out
        assert "<p>" not in out, out
        assert "Hello world!" in out, out
        assert "& more" in out, out

    @check("JSON pass-through")
    def _():
        payload = '{"version": "1.2.3", "deps": []}'
        with patch("rag.agents.tools.web_fetch.httpx.Client",
                   return_value=_client_ctx(
                       _resp(200, payload, "application/json"))):
            out = web_fetch.invoke({"url": "https://api.example.com/p"})
        assert out == payload, out

    @check("HTTP error surfaced")
    def _():
        with patch("rag.agents.tools.web_fetch.httpx.Client",
                   return_value=_client_ctx(_resp(404, "nope"))):
            out = web_fetch.invoke({"url": "https://example.com/x"})
        assert "HTTP 404" in out, out

    @check("transport error caught")
    def _():
        with patch("rag.agents.tools.web_fetch.httpx.Client",
                   side_effect=_httpx.ConnectError("refused")):
            out = web_fetch.invoke({"url": "https://example.com"})
        assert out.startswith("ERROR"), out
        assert "ConnectError" in out, out

    @check("output capped + truncation marker")
    def _():
        big = "<html><body>" + ("A" * 80_000) + "</body></html>"
        with patch("rag.agents.tools.web_fetch.httpx.Client",
                   return_value=_client_ctx(_resp(200, big))):
            out = web_fetch.invoke({"url": "https://example.com/big"})
        assert "[truncated:" in out, out[-200:]
        assert len(out) < 60_000, len(out)


# ── web_search (DDG fallback) ────────────────────────────────────────

_DDG_PAGE = '''
<html><body>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">First &amp; Best</a>
  <a class="result__snippet" href="x">Snippet <b>one</b>.</a>
</div>
<div class="result">
  <a class="result__a" href="https://example.com/b">Second</a>
  <a class="result__snippet" href="x">Snippet two.</a>
</div>
</body></html>
'''


def test_web_search_ddg():
    section("web_search (DDG fallback)")
    import importlib
    ws_mod = importlib.import_module("rag.agents.tools.web_search")
    web_search = ws_mod.web_search
    import httpx as _httpx

    @check("DDG returns parsed list of {url,title,content}")
    def _():
        with patch.object(ws_mod, "TAVILY_API_KEY", ""):
            with patch("rag.agents.tools.web_search.httpx.Client",
                       return_value=_client_ctx(_resp(200, _DDG_PAGE))):
                out = web_search.invoke({"query": "anything"})
        data = json.loads(out)
        assert isinstance(data, list) and len(data) == 2, out
        assert data[0]["title"] == "First & Best", data[0]
        assert data[0]["url"] == "https://example.com/a", data[0]
        assert data[0]["content"] == "Snippet one.", data[0]
        assert data[1]["url"] == "https://example.com/b", data[1]

    @check("empty DDG page → 'no results' message")
    def _():
        with patch.object(ws_mod, "TAVILY_API_KEY", ""):
            with patch("rag.agents.tools.web_search.httpx.Client",
                       return_value=_client_ctx(
                           _resp(200, "<html></html>"))):
                out = web_search.invoke({"query": "nope"})
        assert out.startswith("No web results"), out

    @check("DDG transport error reported")
    def _():
        with patch.object(ws_mod, "TAVILY_API_KEY", ""):
            with patch("rag.agents.tools.web_search.httpx.Client",
                       side_effect=_httpx.ConnectError("dead")):
                out = web_search.invoke({"query": "x"})
        assert out.startswith("Error: web search failed (DDG)"), out


# ── web_search (Tavily branch) ───────────────────────────────────────

def test_web_search_tavily():
    section("web_search (Tavily branch)")
    import importlib
    ws_mod = importlib.import_module("rag.agents.tools.web_search")
    web_search = ws_mod.web_search

    fake_client = MagicMock()
    fake_client.search.return_value = {"results": [
        {"url": "https://t.example/1", "title": "Tav 1",
         "content": "tav content one"},
    ]}
    fake_module = MagicMock()
    fake_module.TavilyClient.return_value = fake_client

    @check("Tavily branch used when key set")
    def _():
        with patch.object(ws_mod, "TAVILY_API_KEY", "fake-key"):
            with patch.dict(sys.modules, {"tavily": fake_module}):
                out = web_search.invoke({"query": "q"})
        data = json.loads(out)
        assert len(data) == 1, out
        assert data[0]["url"] == "https://t.example/1", data[0]

    @check("Tavily error reported, not raised")
    def _():
        fake_client.search.side_effect = RuntimeError("rate limited")
        with patch.object(ws_mod, "TAVILY_API_KEY", "fake-key"):
            with patch.dict(sys.modules, {"tavily": fake_module}):
                out = web_search.invoke({"query": "q"})
        assert out.startswith("Error: web search failed (Tavily)"), out


# ── superdev wiring ──────────────────────────────────────────────────

def test_superdev_wiring():
    section("superdev exposes web_search + web_fetch")
    from rag.agents.tools import TOOLS_BY_NAME

    @check("web_fetch in TOOLS_BY_NAME")
    def _():
        assert "web_fetch" in TOOLS_BY_NAME

    @check("web_search in TOOLS_BY_NAME")
    def _():
        assert "web_search" in TOOLS_BY_NAME

    @check("build_host_tools includes web_search + web_fetch")
    def _():
        from rag.agents.tools.host import build_host_tools
        tools = build_host_tools(wt=None,
                                 allowed_dirs=[Path(".").resolve()])
        names = {t.name for t in tools}
        assert "web_search" in names, sorted(names)
        assert "web_fetch" in names, sorted(names)


def main() -> int:
    test_web_fetch()
    test_web_search_ddg()
    test_web_search_tavily()
    test_superdev_wiring()
    fails = [r for r in results if not r[1]]
    print("\n" + "─" * 60)
    if fails:
        print(f"\033[91m✗ {len(fails)}/{len(results)} failed\033[0m  "
              f"({len(results) - len(fails)} passed)")
        for n, _, d, _ in fails:
            print(f"  \033[91m✗\033[0m {n}: {d}")
        return 1
    print(f"\033[92m✓ {len(results)}/{len(results)} passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
