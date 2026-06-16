"""
MCP resources + prompts tests — no live MCP server.

Mocks `MultiServerMCPClient.session()` as an async context manager
that yields a fake ClientSession exposing list_resources /
read_resource / list_prompts / get_prompt.

Covers:
  * resources.list_resources tool: JSON shape, regex pattern filter,
    empty result, error pass-through.
  * resources.read_resource tool: text content, binary marker, 50 KB
    cap, error pass-through.
  * prompts.list_prompts tool: JSON shape with argument schema,
    empty result, error pass-through.
  * prompts.use_prompt tool: rendered text concatenation, argument
    forwarding, error pass-through.
  * registry pipeline: capability-gated inclusion (skip when
    cap.resources=False / cap.prompts=False), full toolset shape,
    prefixing applied to factory tools.
"""

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("LITELLM_API_KEY", "sk-test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

import conftest  # noqa: F401

from smoke_test import check, section, results  # noqa: E402


# ── fake client + session helpers ────────────────────────────────────

def _fake_session(*, list_res=None, read_res=None,
                  list_pr=None, get_pr=None, raise_on=None):
    """Build an async-context-manager that yields a session mock.
    Any of the methods can be set to raise via `raise_on={'method':
    Exception(...)}`."""
    sess = MagicMock()

    raise_on = raise_on or {}

    async def _list_resources():
        if "list_resources" in raise_on:
            raise raise_on["list_resources"]
        return list_res

    async def _read_resource(uri):
        if "read_resource" in raise_on:
            raise raise_on["read_resource"]
        return read_res

    async def _list_prompts():
        if "list_prompts" in raise_on:
            raise raise_on["list_prompts"]
        return list_pr

    async def _get_prompt(name, arguments):
        if "get_prompt" in raise_on:
            raise raise_on["get_prompt"]
        # Echo arguments through so tests can verify forwarding.
        sess._last_args = (name, arguments)
        return get_pr

    sess.list_resources = _list_resources
    sess.read_resource = _read_resource
    sess.list_prompts = _list_prompts
    sess.get_prompt = _get_prompt

    cm = MagicMock()

    async def _aenter(*a, **k):
        return sess

    async def _aexit(*a, **k):
        return False

    cm.__aenter__ = _aenter
    cm.__aexit__ = _aexit
    return sess, cm


def _fake_client(cm):
    """Build a `client` stand-in whose .session(name) returns the
    given context manager regardless of server name."""
    c = MagicMock()
    c.session = MagicMock(return_value=cm)
    return c


# ── resources ────────────────────────────────────────────────────────

def test_list_resources_tool():
    section("resources.list_resources tool")
    from rag.mcp.resources import _make_list_tool

    list_res = SimpleNamespace(resources=[
        SimpleNamespace(
            uri="file:///etc/hosts", name="hosts",
            mimeType="text/plain",
            description="local DNS overrides"),
        SimpleNamespace(
            uri="file:///var/log/app.log", name="applog",
            mimeType="text/plain", description="app log"),
    ])
    _, cm = _fake_session(list_res=list_res)
    client = _fake_client(cm)
    tool = _make_list_tool("fs", client)

    @check("returns JSON list of {uri, name, mimeType, description}")
    def _():
        out = asyncio.run(tool.ainvoke({"pattern": ""}))
        data = json.loads(out)
        assert len(data) == 2, out
        uris = {x["uri"] for x in data}
        assert "file:///etc/hosts" in uris, uris

    @check("regex pattern filters by URI")
    def _():
        out = asyncio.run(tool.ainvoke({"pattern": r"\.log$"}))
        data = json.loads(out)
        assert len(data) == 1, out
        assert data[0]["name"] == "applog", data

    @check("empty result returns '[]'")
    def _():
        empty_res = SimpleNamespace(resources=[])
        _, cm2 = _fake_session(list_res=empty_res)
        t2 = _make_list_tool("fs", _fake_client(cm2))
        out = asyncio.run(t2.ainvoke({"pattern": ""}))
        assert out == "[]", out

    @check("session error → ERROR string, not raise")
    def _():
        _, cm3 = _fake_session(raise_on={
            "list_resources": RuntimeError("boom")})
        t3 = _make_list_tool("fs", _fake_client(cm3))
        out = asyncio.run(t3.ainvoke({"pattern": ""}))
        assert out.startswith("ERROR: RuntimeError"), out


def test_read_resource_tool():
    section("resources.read_resource tool")
    from rag.mcp.resources import _make_read_tool, _CAP

    @check("text content returned verbatim")
    def _():
        read_res = SimpleNamespace(contents=[
            SimpleNamespace(text="hello world")])
        _, cm = _fake_session(read_res=read_res)
        tool = _make_read_tool("fs", _fake_client(cm))
        out = asyncio.run(tool.ainvoke({"uri": "file:///x"}))
        assert out == "hello world", out

    @check("multiple content parts concatenated with newlines")
    def _():
        read_res = SimpleNamespace(contents=[
            SimpleNamespace(text="part1"),
            SimpleNamespace(text="part2")])
        _, cm = _fake_session(read_res=read_res)
        tool = _make_read_tool("fs", _fake_client(cm))
        out = asyncio.run(tool.ainvoke({"uri": "file:///x"}))
        assert out == "part1\npart2", out

    @check("binary content surfaced as <binary N bytes> marker")
    def _():
        # Object lacking `.text` but having `.blob` triggers binary path.
        c = type("C", (), {})()
        c.blob = b"binary data here"
        read_res = SimpleNamespace(contents=[c])
        _, cm = _fake_session(read_res=read_res)
        tool = _make_read_tool("fs", _fake_client(cm))
        out = asyncio.run(tool.ainvoke({"uri": "file:///b"}))
        assert out == f"<binary {len(b'binary data here')} bytes>", out

    @check("output capped + truncation marker")
    def _():
        big = "A" * (_CAP + 5000)
        read_res = SimpleNamespace(contents=[
            SimpleNamespace(text=big)])
        _, cm = _fake_session(read_res=read_res)
        tool = _make_read_tool("fs", _fake_client(cm))
        out = asyncio.run(tool.ainvoke({"uri": "file:///big"}))
        assert "[truncated:" in out, out[-100:]
        assert len(out) < _CAP + 200, len(out)

    @check("session error → ERROR string, not raise")
    def _():
        _, cm = _fake_session(raise_on={
            "read_resource": ValueError("nope")})
        tool = _make_read_tool("fs", _fake_client(cm))
        out = asyncio.run(tool.ainvoke({"uri": "file:///x"}))
        assert out.startswith("ERROR: ValueError"), out


# ── prompts ──────────────────────────────────────────────────────────

def test_list_prompts_tool():
    section("prompts.list_prompts tool")
    from rag.mcp.prompts import _make_list_tool

    list_pr = SimpleNamespace(prompts=[
        SimpleNamespace(
            name="summarize_issue",
            description="Summarize one GH issue",
            arguments=[
                SimpleNamespace(name="issue_id",
                                description="issue number",
                                required=True),
                SimpleNamespace(name="style", description="tone",
                                required=False),
            ]),
    ])

    @check("returns JSON with argument schema")
    def _():
        _, cm = _fake_session(list_pr=list_pr)
        tool = _make_list_tool("gh", _fake_client(cm))
        out = asyncio.run(tool.ainvoke({}))
        data = json.loads(out)
        assert len(data) == 1, out
        p = data[0]
        assert p["name"] == "summarize_issue", p
        assert {a["name"] for a in p["arguments"]} == {
            "issue_id", "style"}, p
        req = next(a for a in p["arguments"]
                   if a["name"] == "issue_id")
        assert req["required"] is True, req

    @check("empty list returns '[]'")
    def _():
        _, cm = _fake_session(
            list_pr=SimpleNamespace(prompts=[]))
        tool = _make_list_tool("gh", _fake_client(cm))
        out = asyncio.run(tool.ainvoke({}))
        assert out == "[]", out

    @check("session error → ERROR string")
    def _():
        _, cm = _fake_session(raise_on={
            "list_prompts": RuntimeError("dead")})
        tool = _make_list_tool("gh", _fake_client(cm))
        out = asyncio.run(tool.ainvoke({}))
        assert out.startswith("ERROR: RuntimeError"), out


def test_use_prompt_tool():
    section("prompts.use_prompt tool")
    from rag.mcp.prompts import _make_use_tool

    @check("text content concatenated with double newlines")
    def _():
        msg1 = SimpleNamespace(
            content=SimpleNamespace(text="step 1"))
        msg2 = SimpleNamespace(
            content=SimpleNamespace(text="step 2"))
        get_pr = SimpleNamespace(messages=[msg1, msg2])
        sess, cm = _fake_session(get_pr=get_pr)
        tool = _make_use_tool("gh", _fake_client(cm))
        out = asyncio.run(tool.ainvoke({
            "name": "p1", "arguments": {"x": 1}}))
        assert out == "step 1\n\nstep 2", out
        assert sess._last_args == ("p1", {"x": 1}), sess._last_args

    @check("None arguments → empty dict forwarded")
    def _():
        get_pr = SimpleNamespace(messages=[
            SimpleNamespace(content=SimpleNamespace(text="ok"))])
        sess, cm = _fake_session(get_pr=get_pr)
        tool = _make_use_tool("gh", _fake_client(cm))
        asyncio.run(tool.ainvoke({"name": "p1"}))
        assert sess._last_args[1] == {}, sess._last_args

    @check("plain-string content supported")
    def _():
        get_pr = SimpleNamespace(messages=[
            SimpleNamespace(content="raw text")])
        _, cm = _fake_session(get_pr=get_pr)
        tool = _make_use_tool("gh", _fake_client(cm))
        out = asyncio.run(tool.ainvoke({"name": "p1"}))
        assert out == "raw text", out

    @check("empty messages → '(empty prompt)'")
    def _():
        get_pr = SimpleNamespace(messages=[])
        _, cm = _fake_session(get_pr=get_pr)
        tool = _make_use_tool("gh", _fake_client(cm))
        out = asyncio.run(tool.ainvoke({"name": "p1"}))
        assert out == "(empty prompt)", out

    @check("session error → ERROR string")
    def _():
        _, cm = _fake_session(raise_on={
            "get_prompt": Exception("boom")})
        tool = _make_use_tool("gh", _fake_client(cm))
        out = asyncio.run(tool.ainvoke({"name": "p1"}))
        assert out.startswith("ERROR:"), out


# ── registry capability gating ──────────────────────────────────────

def test_registry_capability_gating():
    section("registry capability gating")
    from rag.mcp import client as mcp_client
    from rag.mcp.config import ServerSpec
    from rag.mcp.registry import get_mcp_tools

    def _make_native():
        from langchain_core.tools import StructuredTool

        def f(x: str = "") -> str:
            return x

        return [StructuredTool.from_function(
            func=f, name="ping", description="d")]

    # Build a fake client whose session() works for any name.
    _, cm = _fake_session(
        list_res=SimpleNamespace(resources=[]),
        read_res=SimpleNamespace(contents=[]),
        list_pr=SimpleNamespace(prompts=[]),
        get_pr=SimpleNamespace(messages=[]))
    fake_client = _fake_client(cm)

    spec = ServerSpec(name="srv", transport="stdio", connection={},
                      source="test")

    def _setup(caps_map):
        mcp_client._specs = {"srv": spec}
        mcp_client._tools_cache = {"srv": _make_native()}
        mcp_client._caps = caps_map
        mcp_client._client = fake_client

    os.environ.pop("MCP_ENABLED", None)

    @check("no caps → native tools only (no list/read/use_prompt)")
    def _():
        _setup({"srv": {"resources": False, "prompts": False}})
        names = {t.name for t in get_mcp_tools()}
        assert "srv__ping" in names, names
        assert not any(n.endswith("__list_resources") for n in names), names
        assert not any(n.endswith("__list_prompts") for n in names), names

    @check("resources cap → +list_resources +read_resource")
    def _():
        _setup({"srv": {"resources": True, "prompts": False}})
        names = {t.name for t in get_mcp_tools()}
        assert "srv__list_resources" in names, names
        assert "srv__read_resource" in names, names
        assert "srv__list_prompts" not in names, names

    @check("prompts cap → +list_prompts +use_prompt")
    def _():
        _setup({"srv": {"resources": False, "prompts": True}})
        names = {t.name for t in get_mcp_tools()}
        assert "srv__list_prompts" in names, names
        assert "srv__use_prompt" in names, names
        assert "srv__list_resources" not in names, names

    @check("both caps → all 5 tools (1 native + 2 res + 2 prompt)")
    def _():
        _setup({"srv": {"resources": True, "prompts": True}})
        names = {t.name for t in get_mcp_tools()}
        assert names == {
            "srv__ping",
            "srv__list_resources", "srv__read_resource",
            "srv__list_prompts", "srv__use_prompt",
        }, names

    @check("client=None → only native tools (no factory tools)")
    def _():
        _setup({"srv": {"resources": True, "prompts": True}})
        mcp_client._client = None
        names = {t.name for t in get_mcp_tools()}
        assert names == {"srv__ping"}, names


def main() -> int:
    test_list_resources_tool()
    test_read_resource_tool()
    test_list_prompts_tool()
    test_use_prompt_tool()
    test_registry_capability_gating()
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
