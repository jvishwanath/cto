"""
MCP client lifecycle.

Wraps langchain_mcp_adapters.MultiServerMCPClient with:
  * Lazy single-process pool (constructed at first use; reused).
  * Fail-soft connect — one dead server doesn't sink the others.
  * Idempotent shutdown.
  * `enabled` env-flag guard (MCP_ENABLED=false → no-op everywhere).

The public surface used by the rest of the codebase is intentionally
narrow: `get_client()`, `connect_all()`, `shutdown_all()`, `specs()`.
Tool / resource / prompt extraction lives in registry.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from langchain_core.tools import BaseTool

from .config import ServerSpec, load_config

log = logging.getLogger(__name__)

_ENABLED_ENV = "MCP_ENABLED"

# Lazy module-level state. Single-tenant deploy → app-wide singleton.
_specs: dict[str, ServerSpec] = {}
_client: Any = None                     # MultiServerMCPClient | None
_connected: bool = False
_lock = asyncio.Lock()
# Per-server tool list cached at connect_all so the synchronous
# registry/host wiring can read without awaiting.
_tools_cache: dict[str, list[BaseTool]] = {}
# Per-server capability flags probed at connect_all. False = the
# server returned "method not found" / similar; we suppress the
# corresponding tool wrappers to keep the agent toolset lean.
_caps: dict[str, dict[str, bool]] = {}


def is_enabled() -> bool:
    return os.environ.get(_ENABLED_ENV, "true").lower() != "false"


def specs() -> dict[str, ServerSpec]:
    """Return the parsed config (loaded on first call). Empty when
    MCP is disabled or no config files are present."""
    global _specs
    if not is_enabled():
        return {}
    if not _specs:
        _specs = load_config()
    return _specs


def _build_client(servers: dict[str, ServerSpec]) -> Any:
    """Construct the MultiServerMCPClient with the connection dicts
    drawn from our ServerSpec objects. Tool-name prefixing is OFF —
    we apply our own `<server>__<tool>` prefix in tools.py so the
    layout stays consistent across tools / resources / prompts.

    Runtime callbacks (progress / server logging) wired in via
    events.build_callbacks so the agent surfaces long-tool-call
    progress live instead of looking hung."""
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from .events import build_callbacks
    connections = {name: spec.connection
                   for name, spec in servers.items()}
    return MultiServerMCPClient(
        connections=connections,
        tool_name_prefix=False,
        handle_tool_errors=True,
        callbacks=build_callbacks(),
    )


async def get_client() -> Any | None:
    """Return the singleton MultiServerMCPClient (constructing it on
    first call). Returns None when MCP is disabled or no servers
    parsed."""
    global _client
    if not is_enabled():
        return None
    async with _lock:
        if _client is None:
            servers = specs()
            if not servers:
                return None
            try:
                _client = _build_client(servers)
            except Exception as e:
                log.exception("[mcp] client construction failed: %s", e)
                return None
        return _client


def tools_cache() -> dict[str, list[BaseTool]]:
    """Per-server tool list, unprefixed, populated by connect_all.
    Returned dict is the live cache — callers read but should NOT
    mutate. Empty until connect_all completes (or when MCP is off)."""
    return _tools_cache


def caps() -> dict[str, dict[str, bool]]:
    """{server: {'resources': bool, 'prompts': bool}} probed at
    connect_all. Missing key → server didn't connect."""
    return _caps


async def connect_all() -> None:
    """FastAPI startup hook. Eagerly resolves the singleton AND
    fetches each server's tool list into the cache so subsequent
    sync `registry.get_mcp_tools()` calls don't have to await.
    Fail-soft per server."""
    global _connected
    if not is_enabled():
        log.info("[mcp] disabled (MCP_ENABLED=false)")
        return
    if _connected:
        return
    client = await get_client()
    if client is None:
        log.info("[mcp] no servers configured")
        return
    for name in list(specs()):
        try:
            tools = await client.get_tools(server_name=name)
            _tools_cache[name] = list(tools)
            log.info("[mcp] %s: %d tool(s)", name, len(tools))
        except Exception as e:
            log.warning("[mcp] %s: get_tools failed: %s", name, e)
            _tools_cache[name] = []
        # Probe resource / prompt capability — server may not
        # implement either. A failed probe drops the corresponding
        # tool wrappers (cleaner agent toolset).
        cap = {"resources": False, "prompts": False}
        try:
            async with client.session(name) as sess:
                try:
                    await sess.list_resources()
                    cap["resources"] = True
                except Exception:
                    pass
                try:
                    await sess.list_prompts()
                    cap["prompts"] = True
                except Exception:
                    pass
        except Exception as e:
            log.debug("[mcp] %s: capability probe skipped (%s)",
                      name, e)
        _caps[name] = cap
        if cap["resources"] or cap["prompts"]:
            log.info("[mcp] %s capabilities: %s", name,
                     ", ".join(k for k, v in cap.items() if v))
    _connected = True
    log.info("[mcp] connected (%d server(s))", len(specs()))


async def shutdown_all() -> None:
    """FastAPI shutdown hook. Closes the singleton + clears state so a
    subsequent connect_all (e.g. in tests) starts fresh."""
    global _client, _connected, _specs, _tools_cache, _caps
    client = _client
    _client = None
    _connected = False
    _specs = {}
    _tools_cache = {}
    _caps = {}
    if client is None:
        return
    closer = getattr(client, "close", None) or getattr(
        client, "aclose", None)
    if closer is None:
        return
    try:
        result = closer()
        if asyncio.iscoroutine(result):
            await result
    except Exception as e:
        log.warning("[mcp] shutdown error (ignored): %s", e)
