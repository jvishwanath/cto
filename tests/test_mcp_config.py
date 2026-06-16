"""
MCP config loader tests — no live MCP servers, no langchain-mcp
client construction.

Covers:
  * Layered discovery: project > user > bundled precedence.
  * stdio / sse / streamable_http transport inference.
  * Explicit transport override.
  * env-var interpolation (${VAR}), missing var → server disabled
    with warning (not crash).
  * `enabled: false` skips entry.
  * Per-server allowed_tools / denied_tools / confirm_writes /
    subagent_visible / timeout_ms.
  * Bad entry skipped fail-soft (other servers still load).
  * Lifecycle: is_enabled() honors MCP_ENABLED=false.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LITELLM_API_KEY", "sk-test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

import conftest  # noqa: F401

from smoke_test import check, section, results  # noqa: E402


def _write_config(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ── parse: transport inference ──────────────────────────────────────

def test_transport_inference():
    section("transport inference")
    from rag.mcp.config import load_config

    tmp = Path(tempfile.mkdtemp(prefix="mcp-"))
    try:
        _write_config(tmp / ".mcp.json", {"mcpServers": {
            "stdio-one": {"command": "echo", "args": ["hi"]},
            "http-one": {"url": "https://example.com/mcp"},
            "sse-one": {"url": "https://example.com/sse",
                        "transport": "sse"},
            "forced-streamable": {"url": "https://example.com",
                                  "transport": "streamable-http"},
        }})

        with patch("rag.mcp.config._discovery_roots",
                   return_value=[(tmp / ".mcp.json", "project")]):
            specs = load_config()

        @check("stdio inferred from `command`")
        def _():
            assert specs["stdio-one"].transport == "stdio", \
                specs["stdio-one"]

        @check("streamable_http inferred from `url`")
        def _():
            assert specs["http-one"].transport == "streamable_http", \
                specs["http-one"]

        @check("explicit `transport: sse` honored")
        def _():
            assert specs["sse-one"].transport == "sse", specs["sse-one"]

        @check("dash → underscore: 'streamable-http' → streamable_http")
        def _():
            assert (specs["forced-streamable"].transport
                    == "streamable_http"), specs["forced-streamable"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── env interpolation ───────────────────────────────────────────────

def test_env_interpolation():
    section("env interpolation")
    from rag.mcp.config import load_config

    tmp = Path(tempfile.mkdtemp(prefix="mcp-env-"))
    try:
        _write_config(tmp / ".mcp.json", {"mcpServers": {
            "with-env": {
                "command": "echo",
                "env": {"TOKEN": "${MCP_TEST_TOKEN}"},
            },
            "missing-env": {
                "command": "echo",
                "env": {"TOKEN": "${MCP_DEFINITELY_NOT_SET_XYZ}"},
            },
            "http-with-headers": {
                "url": "https://api.example.com",
                "headers": {"Authorization": "Bearer ${MCP_TEST_TOKEN}"},
            },
        }})

        @check("present var interpolated into env")
        def _():
            with patch.dict(os.environ,
                            {"MCP_TEST_TOKEN": "s3cr3t"},
                            clear=False):
                with patch("rag.mcp.config._discovery_roots",
                           return_value=[(tmp / ".mcp.json", "project")]):
                    specs = load_config()
            assert "with-env" in specs, sorted(specs)
            assert (specs["with-env"].connection["env"]["TOKEN"]
                    == "s3cr3t"), specs["with-env"].connection

        @check("present var interpolated into HTTP headers")
        def _():
            with patch.dict(os.environ,
                            {"MCP_TEST_TOKEN": "s3cr3t"},
                            clear=False):
                with patch("rag.mcp.config._discovery_roots",
                           return_value=[(tmp / ".mcp.json", "project")]):
                    specs = load_config()
            assert (specs["http-with-headers"].connection["headers"]
                    ["Authorization"] == "Bearer s3cr3t")

        @check("missing var disables that server (others still load)")
        def _():
            os.environ.pop("MCP_DEFINITELY_NOT_SET_XYZ", None)
            with patch.dict(os.environ,
                            {"MCP_TEST_TOKEN": "s3cr3t"},
                            clear=False):
                with patch("rag.mcp.config._discovery_roots",
                           return_value=[(tmp / ".mcp.json", "project")]):
                    specs = load_config()
            assert "missing-env" not in specs, sorted(specs)
            assert "with-env" in specs, sorted(specs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── layered discovery ───────────────────────────────────────────────

def test_layered_discovery():
    section("layered discovery + precedence")
    from rag.mcp.config import load_config

    tmp = Path(tempfile.mkdtemp(prefix="mcp-layers-"))
    proj = tmp / "proj" / ".mcp.json"
    user = tmp / "home" / ".mcp.json"
    bund = tmp / "bundled" / "mcp.json"
    try:
        _write_config(proj, {"mcpServers": {
            "shared": {"command": "p-echo"},
            "proj-only": {"command": "p-only"},
        }})
        _write_config(user, {"mcpServers": {
            "shared": {"command": "u-echo"},
            "user-only": {"command": "u-only"},
        }})
        _write_config(bund, {"mcpServers": {
            "shared": {"command": "b-echo"},
            "bund-only": {"command": "b-only"},
        }})

        roots = [(proj, "project"), (user, "user"), (bund, "bundled")]
        with patch("rag.mcp.config._discovery_roots",
                   return_value=roots):
            specs = load_config()

        @check("project wins on collision (`shared`)")
        def _():
            assert specs["shared"].source == "project", specs["shared"]
            assert specs["shared"].connection["command"] == "p-echo"

        @check("all unique entries loaded across layers")
        def _():
            assert {"shared", "proj-only", "user-only", "bund-only"} \
                <= set(specs), sorted(specs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── enabled flag + per-server options ───────────────────────────────

def test_options():
    section("per-server options")
    from rag.mcp.config import load_config

    tmp = Path(tempfile.mkdtemp(prefix="mcp-opts-"))
    try:
        _write_config(tmp / ".mcp.json", {"mcpServers": {
            "disabled": {"command": "echo", "enabled": False},
            "filtered": {
                "command": "echo",
                "allowed_tools": ["a", "b"],
                "denied_tools": ["c"],
                "confirm_writes": False,
                "subagent_visible": True,
                "timeout_ms": 5000,
            },
            "broken-stdio": {"transport": "stdio"},  # no command
            "broken-http": {"transport": "sse"},      # no url
            "bad-name!": {"command": "echo"},
        }})
        with patch("rag.mcp.config._discovery_roots",
                   return_value=[(tmp / ".mcp.json", "project")]):
            specs = load_config()

        @check("enabled:false skips entry")
        def _():
            assert "disabled" not in specs, sorted(specs)

        @check("options round-trip into ServerSpec")
        def _():
            s = specs["filtered"]
            assert s.allowed_tools == ("a", "b"), s.allowed_tools
            assert s.denied_tools == ("c",), s.denied_tools
            assert s.confirm_writes is False
            assert s.subagent_visible is True
            assert s.timeout_ms == 5000

        @check("broken stdio (no command) skipped fail-soft")
        def _():
            assert "broken-stdio" not in specs, sorted(specs)

        @check("broken http (no url) skipped fail-soft")
        def _():
            assert "broken-http" not in specs, sorted(specs)

        @check("invalid name skipped fail-soft")
        def _():
            assert "bad-name!" not in specs, sorted(specs)

        @check("valid entries still loaded after bad ones")
        def _():
            assert "filtered" in specs, sorted(specs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── client lifecycle ────────────────────────────────────────────────

def test_client_lifecycle():
    section("client lifecycle gating")
    import asyncio

    @check("MCP_ENABLED=false → is_enabled False")
    def _():
        from rag.mcp import client as mcp_client
        with patch.dict(os.environ, {"MCP_ENABLED": "false"},
                        clear=False):
            assert mcp_client.is_enabled() is False

    @check("MCP_ENABLED unset → is_enabled True (default on)")
    def _():
        from rag.mcp import client as mcp_client
        os.environ.pop("MCP_ENABLED", None)
        assert mcp_client.is_enabled() is True

    @check("no config files → get_client returns None")
    def _():
        from rag.mcp import client as mcp_client
        # Reset module-level cache between tests.
        mcp_client._specs = {}
        mcp_client._client = None
        mcp_client._connected = False
        empty_root = Path(tempfile.mkdtemp(prefix="mcp-empty-"))
        try:
            with patch("rag.mcp.config._discovery_roots",
                       return_value=[(empty_root / ".mcp.json",
                                      "project")]):
                got = asyncio.run(mcp_client.get_client())
            assert got is None, got
        finally:
            shutil.rmtree(empty_root, ignore_errors=True)

    @check("disabled → connect_all is a no-op (no exception)")
    def _():
        from rag.mcp import client as mcp_client
        mcp_client._specs = {}
        mcp_client._client = None
        mcp_client._connected = False
        with patch.dict(os.environ, {"MCP_ENABLED": "false"},
                        clear=False):
            asyncio.run(mcp_client.connect_all())
            asyncio.run(mcp_client.shutdown_all())
        assert mcp_client._client is None

    @check("registry stubs return empty lists")
    def _():
        from rag.mcp.registry import get_mcp_tools, get_mcp_prompts
        assert get_mcp_tools() == []
        assert get_mcp_prompts() == []


def main() -> int:
    test_transport_inference()
    test_env_interpolation()
    test_layered_discovery()
    test_options()
    test_client_lifecycle()
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
