"""
MCP resources surfaced as agent tools.

For every MCP server whose `resources` capability was detected at
connect_all, registry.get_mcp_tools adds TWO tools to the superdev
toolset:

  <server>__list_resources(pattern: str = "")
      → JSON [{uri, name, mimeType, description}, …]
  <server>__read_resource(uri: str)
      → text content (binary → "<binary N bytes>"; capped at ~50 KB)

The agent treats these as a virtual filesystem over the server's
data (filesystem files, sqlite rows, sentry events, …).

Both tools open a fresh client.session per call. Sessions are cheap
once the underlying transport is up — no need to long-hold one.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

log = logging.getLogger(__name__)

_CAP = 50_000


def _make_list_tool(server: str, client: Any) -> BaseTool:

    async def list_resources(pattern: str = "") -> str:
        try:
            async with client.session(server) as sess:
                res = await sess.list_resources()
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"
        rx = re.compile(pattern) if pattern else None
        items: list[dict] = []
        for r in res.resources:
            uri = str(r.uri)
            if rx and not rx.search(uri):
                continue
            items.append({
                "uri": uri,
                "name": r.name or "",
                "mimeType": r.mimeType or "",
                "description": (r.description or "")[:200],
            })
        return json.dumps(items, indent=2) if items else "[]"

    return StructuredTool.from_function(
        coroutine=list_resources,
        name="list_resources",
        description=(
            f"List resources exposed by the `{server}` MCP server. "
            "Returns JSON [{uri, name, mimeType, description}]. "
            "Optional `pattern` is a regex applied to URI."))


def _make_read_tool(server: str, client: Any) -> BaseTool:

    async def read_resource(uri: str) -> str:
        try:
            async with client.session(server) as sess:
                res = await sess.read_resource(uri)
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"
        parts: list[str] = []
        for c in res.contents:
            text = getattr(c, "text", None)
            if text is not None:
                parts.append(text)
                continue
            blob = getattr(c, "blob", None)
            if blob is not None:
                parts.append(f"<binary {len(blob)} bytes>")
        body = "\n".join(parts)
        if len(body) > _CAP:
            body = body[:_CAP] + (
                f"\n\n[truncated: {len(body) - _CAP} more bytes]")
        return body

    return StructuredTool.from_function(
        coroutine=read_resource,
        name="read_resource",
        description=(
            f"Read one resource from the `{server}` MCP server by "
            "URI. Output is text (binary → `<binary N bytes>` "
            "marker); capped at ~50 KB."))


def build_resource_tools(server: str, client: Any) -> list[BaseTool]:
    """Return the [list_resources, read_resource] pair for one
    server. Tools are bound to `client` + `server` via closure so
    they call through to the right MCP backend without further
    plumbing."""
    return [_make_list_tool(server, client),
            _make_read_tool(server, client)]
