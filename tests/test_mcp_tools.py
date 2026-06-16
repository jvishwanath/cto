"""
MCP tool wrapping tests — no live MCP server, no client init.

Covers:
  * prefix(): `<server>__<tool>` rename, underlying invocation
    preserved (async + sync).
  * looks_mutating(): true/false heuristic on real tool names.
  * wrap_confirm(): deny → skip message, no inner call; approve →
    inner call result. Both async and sync paths.
  * filter_allowed(): allow / deny semantics, empty allow = pass-all.
  * registry.get_mcp_tools(): full pipeline — filtering, confirm
    wrap, prefix; subagent (child_mode) hides non-visible servers
    and never wraps in confirm; empty cache → [].
  * host.build_host_tools splices MCP tools into superdev base.
"""

import asyncio
import os
import sys
from unittest.mock import patch

os.environ.setdefault("LITELLM_API_KEY", "sk-test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

import conftest  # noqa: F401

from smoke_test import check, section, results  # noqa: E402


# ── fixtures: fake MCP tools ─────────────────────────────────────────

def _make_tools():
    from langchain_core.tools import StructuredTool

    def _sync_list(path: str = ".") -> str:
        return f"listed {path}"

    async def _async_create(name: str) -> str:
        return f"created {name}"

    def _sync_read(uri: str) -> str:
        return f"read {uri}"

    async def _async_delete(target: str) -> str:
        return f"deleted {target}"

    return [
        StructuredTool.from_function(
            func=_sync_list, name="list_dir",
            description="list files"),
        StructuredTool.from_function(
            coroutine=_async_create, name="create_issue",
            description="create"),
        StructuredTool.from_function(
            func=_sync_read, name="read_resource",
            description="read"),
        StructuredTool.from_function(
            coroutine=_async_delete, name="delete_record",
            description="delete"),
    ]


# ── prefix ───────────────────────────────────────────────────────────

def test_prefix():
    section("prefix")
    from rag.mcp.tools import prefix, prefix_name

    @check("name composed `<server>__<tool>`")
    def _():
        assert prefix_name("fs", "read") == "fs__read"

    @check("prefix preserves sync invocation")
    def _():
        tools = _make_tools()
        p = prefix(tools[0], "fs")
        assert p.name == "fs__list_dir", p.name
        assert p.invoke({"path": "/tmp"}) == "listed /tmp", p.invoke({})

    @check("prefix preserves async invocation")
    def _():
        tools = _make_tools()
        p = prefix(tools[1], "gh")
        assert p.name == "gh__create_issue"
        result = asyncio.run(p.ainvoke({"name": "bug"}))
        assert result == "created bug", result

    @check("prefix doesn't mutate original tool")
    def _():
        tools = _make_tools()
        p = prefix(tools[0], "fs")
        assert tools[0].name == "list_dir", tools[0].name
        assert p.name != tools[0].name


# ── looks_mutating ───────────────────────────────────────────────────

def test_looks_mutating():
    section("looks_mutating heuristic")
    from langchain_core.tools import StructuredTool
    from rag.mcp.tools import looks_mutating

    def _f():
        return "x"

    def mk(name):
        return StructuredTool.from_function(
            func=_f, name=name, description="d")

    @check("write-y names match")
    def _():
        for name in ("create_issue", "delete_record", "write_file",
                     "update_user", "push_branch", "merge_pr",
                     "set_topic", "patch_config", "remove_member",
                     "destroy_index", "rename_repo"):
            assert looks_mutating(mk(name)), name

    @check("read-y names don't match")
    def _():
        for name in ("list_dir", "read_resource", "get_user",
                     "describe_table", "search_issues", "lookup_dns",
                     "fetch_page"):
            assert not looks_mutating(mk(name)), name


# ── wrap_confirm ─────────────────────────────────────────────────────

def test_wrap_confirm():
    section("wrap_confirm")
    from rag.mcp.tools import wrap_confirm

    @check("deny returns skip message, never calls inner (sync)")
    def _():
        tools = _make_tools()
        calls = []

        def yes_no(label, preview):
            calls.append((label, preview))
            return False

        # Wrap a sync tool — list_dir is sync though not mutating;
        # behaviour is the same regardless of mutation heuristic.
        wrapped = wrap_confirm(tools[0], "fs", yes_no)
        out = wrapped.invoke({"path": "/x"})
        assert "Skipped by user" in out, out
        assert "fs/list_dir" in out, out
        assert calls == [("fs/list_dir", "path='/x'")], calls

    @check("approve passes through to inner result (sync)")
    def _():
        tools = _make_tools()
        wrapped = wrap_confirm(tools[0], "fs",
                               lambda _l, _p: True)
        assert wrapped.invoke({"path": "/x"}) == "listed /x"

    @check("deny on async wrapper returns skip without await")
    def _():
        tools = _make_tools()
        wrapped = wrap_confirm(tools[1], "gh",
                               lambda _l, _p: False)
        out = asyncio.run(wrapped.ainvoke({"name": "bug"}))
        assert "Skipped by user" in out and "gh/create_issue" in out

    @check("approve on async wrapper awaits inner")
    def _():
        tools = _make_tools()
        wrapped = wrap_confirm(tools[1], "gh",
                               lambda _l, _p: True)
        out = asyncio.run(wrapped.ainvoke({"name": "bug"}))
        assert out == "created bug", out

    @check("long args preview truncated")
    def _():
        tools = _make_tools()
        captured = {}

        def cap(label, preview):
            captured["preview"] = preview
            return False

        wrapped = wrap_confirm(tools[0], "fs", cap)
        wrapped.invoke({"path": "x" * 200})
        assert "…" in captured["preview"], captured["preview"]
        assert len(captured["preview"]) < 110, len(captured["preview"])


# ── filter_allowed ───────────────────────────────────────────────────

def test_filter():
    section("filter_allowed")
    from rag.mcp.tools import filter_allowed

    @check("empty allow = pass-all, denied removed")
    def _():
        tools = _make_tools()
        out = filter_allowed(tools, (), ("read_resource",))
        names = [t.name for t in out]
        assert "read_resource" not in names, names
        assert len(out) == 3, names

    @check("non-empty allow restricts")
    def _():
        tools = _make_tools()
        out = filter_allowed(tools,
                             ("list_dir", "create_issue"), ())
        names = sorted(t.name for t in out)
        assert names == ["create_issue", "list_dir"], names

    @check("deny wins over allow")
    def _():
        tools = _make_tools()
        out = filter_allowed(tools,
                             ("list_dir", "delete_record"),
                             ("delete_record",))
        names = [t.name for t in out]
        assert names == ["list_dir"], names


# ── registry.get_mcp_tools pipeline ─────────────────────────────────

def test_registry_pipeline():
    section("registry.get_mcp_tools pipeline")
    from rag.mcp import client as mcp_client
    from rag.mcp.config import ServerSpec
    from rag.mcp.registry import get_mcp_tools

    def _setup(specs_map, cache_map):
        mcp_client._specs = specs_map
        mcp_client._tools_cache = cache_map

    tools_fs = _make_tools()
    tools_gh = _make_tools()
    fs_spec = ServerSpec(
        name="fs", transport="stdio", connection={},
        allowed_tools=(), denied_tools=(),
        confirm_writes=True, subagent_visible=False,
        source="test")
    gh_spec = ServerSpec(
        name="gh", transport="stdio", connection={},
        allowed_tools=(), denied_tools=(),
        confirm_writes=True, subagent_visible=True,
        source="test")

    @check("empty cache → []")
    def _():
        _setup({}, {})
        assert get_mcp_tools() == []

    @check("MCP_ENABLED=false → []")
    def _():
        _setup({"fs": fs_spec}, {"fs": tools_fs})
        with patch.dict(os.environ, {"MCP_ENABLED": "false"},
                        clear=False):
            assert get_mcp_tools(
                confirm=lambda *_: True) == []

    @check("parent mode: every tool prefixed with `<server>__`")
    def _():
        os.environ.pop("MCP_ENABLED", None)
        _setup({"fs": fs_spec, "gh": gh_spec},
               {"fs": tools_fs, "gh": tools_gh})
        out = get_mcp_tools(confirm=lambda *_: True)
        names = {t.name for t in out}
        assert all("__" in n for n in names), names
        assert "fs__list_dir" in names, names
        assert "gh__create_issue" in names, names

    @check("mutating tools wrapped → deny short-circuits")
    def _():
        _setup({"fs": fs_spec}, {"fs": tools_fs})
        out = get_mcp_tools(confirm=lambda _l, _p: False)
        by_name = {t.name: t for t in out}
        # delete_record matches mutate regex → wrapped
        skipped = by_name["fs__delete_record"]
        result = asyncio.run(skipped.ainvoke({"target": "x"}))
        assert "Skipped by user" in result, result
        # list_dir does NOT match → invokes normally
        normal = by_name["fs__list_dir"]
        assert normal.invoke({"path": "/y"}) == "listed /y"

    @check("server with confirm_writes=False bypasses gate")
    def _():
        spec_off = ServerSpec(
            name="fs", transport="stdio", connection={},
            confirm_writes=False, source="test")
        _setup({"fs": spec_off}, {"fs": tools_fs})
        out = get_mcp_tools(confirm=lambda _l, _p: False)
        by_name = {t.name: t for t in out}
        # delete_record still invokes — confirm is bypassed
        result = asyncio.run(
            by_name["fs__delete_record"].ainvoke({"target": "z"}))
        assert result == "deleted z", result

    @check("child_mode hides servers without subagent_visible")
    def _():
        _setup({"fs": fs_spec, "gh": gh_spec},
               {"fs": tools_fs, "gh": tools_gh})
        out = get_mcp_tools(child_mode=True)
        names = {t.name for t in out}
        assert all(n.startswith("gh__") for n in names), names
        assert not any(n.startswith("fs__") for n in names), names

    @check("child_mode: visible server's mutating tools NOT wrapped")
    def _():
        _setup({"gh": gh_spec}, {"gh": tools_gh})
        out = get_mcp_tools(child_mode=True, confirm=None)
        by_name = {t.name: t for t in out}
        # No confirm fn passed → must invoke directly even on
        # mutating tool.
        result = asyncio.run(
            by_name["gh__create_issue"].ainvoke({"name": "bug"}))
        assert result == "created bug", result

    @check("allowed_tools / denied_tools applied (unprefixed)")
    def _():
        spec_filt = ServerSpec(
            name="fs", transport="stdio", connection={},
            allowed_tools=("list_dir", "delete_record"),
            denied_tools=("delete_record",), source="test")
        _setup({"fs": spec_filt}, {"fs": tools_fs})
        out = get_mcp_tools(confirm=lambda *_: True)
        names = {t.name for t in out}
        assert names == {"fs__list_dir"}, names


# ── host.build_host_tools wiring ────────────────────────────────────

def test_host_splice():
    section("host.build_host_tools splices MCP tools")
    from pathlib import Path
    from rag.agents.tools.host import build_host_tools
    from rag.mcp import client as mcp_client
    from rag.mcp.config import ServerSpec

    spec = ServerSpec(
        name="fs", transport="stdio", connection={},
        source="test")
    mcp_client._specs = {"fs": spec}
    mcp_client._tools_cache = {"fs": _make_tools()}
    os.environ.pop("MCP_ENABLED", None)

    @check("parent mode: MCP tools appear in toolset")
    def _():
        tools = build_host_tools(
            wt=None, allowed_dirs=[Path(".").resolve()])
        names = {t.name for t in tools}
        assert "fs__list_dir" in names, sorted(names)
        assert "fs__delete_record" in names, sorted(names)

    @check("child_mode + subagent_visible=False: MCP tools hidden")
    def _():
        tools = build_host_tools(
            wt=None, allowed_dirs=[Path(".").resolve()],
            child_mode=True)
        names = {t.name for t in tools}
        assert not any(n.startswith("fs__") for n in names), \
            sorted(names)


def main() -> int:
    test_prefix()
    test_looks_mutating()
    test_wrap_confirm()
    test_filter()
    test_registry_pipeline()
    test_host_splice()
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
