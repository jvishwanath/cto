"""
MCP prompts surfaced as agent tools.

For every MCP server whose `prompts` capability was detected at
connect_all, registry.get_mcp_tools adds TWO tools to the superdev
toolset:

  <server>__list_prompts()
      → JSON [{name, description, arguments: [{name, required}]}, …]
  <server>__use_prompt(name: str, arguments: dict | None)
      → rendered text content (concatenation of all returned
        messages); the agent typically uses this to seed its next
        turn with a server-curated workflow

This is the conservative v1 surface: prompts stay agent-callable
tools, not slash-invokable skills. The pre-rendering / slash-skill
bridge is deferred (see docs/MCP.md "v1 non-scope"); doing it well
requires async splice support in superdev_graph which isn't worth
the complexity until users ask.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

log = logging.getLogger(__name__)


def _make_list_tool(server: str, client: Any) -> BaseTool:

    async def list_prompts() -> str:
        try:
            async with client.session(server) as sess:
                res = await sess.list_prompts()
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"
        items: list[dict] = []
        for p in res.prompts:
            args = []
            for a in (p.arguments or []):
                args.append({
                    "name": a.name,
                    "description": (a.description or "")[:200],
                    "required": bool(getattr(a, "required", False)),
                })
            items.append({
                "name": p.name,
                "description": (p.description or "")[:200],
                "arguments": args,
            })
        return json.dumps(items, indent=2) if items else "[]"

    return StructuredTool.from_function(
        coroutine=list_prompts,
        name="list_prompts",
        description=(
            f"List server-curated prompts exposed by `{server}`. "
            "Returns JSON [{name, description, arguments}]. Pair "
            "with `use_prompt` to invoke one."))


def _make_use_tool(server: str, client: Any) -> BaseTool:

    async def use_prompt(name: str,
                         arguments: dict[str, Any] | None = None
                         ) -> str:
        try:
            async with client.session(server) as sess:
                res = await sess.get_prompt(
                    name=name, arguments=arguments or {})
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"
        parts: list[str] = []
        for m in res.messages:
            content = m.content
            if hasattr(content, "text"):
                parts.append(content.text)
            elif isinstance(content, list):
                for c in content:
                    if hasattr(c, "text"):
                        parts.append(c.text)
            elif isinstance(content, str):
                parts.append(content)
        return "\n\n".join(parts) if parts else "(empty prompt)"

    return StructuredTool.from_function(
        coroutine=use_prompt,
        name="use_prompt",
        description=(
            f"Render a server-curated prompt from `{server}`. Pass "
            "`name` (from list_prompts) plus `arguments` (dict). "
            "Returns the rendered text — use it as guidance for "
            "your next step."))


def build_prompt_tools(server: str, client: Any) -> list[BaseTool]:
    return [_make_list_tool(server, client),
            _make_use_tool(server, client)]
