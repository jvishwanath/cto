"""
MCP tool wrapping: name prefixing, destructive-gate confirmation,
per-server allow / deny filter.

The MCP spec doesn't mark tools as read vs write — we infer
"mutating" from the tool name (`_MUTATE_RE`). False positives are
preferable to false negatives: an extra confirmation is annoying,
an un-gated destructive call can corrupt state.

This module is pure — no I/O, no client state. The registry calls
into here with cached tool lists from client.tools_cache().
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Callable

from langchain_core.tools import BaseTool

log = logging.getLogger(__name__)


def _bridge_sync(coro_fn: Callable):
    """Build a sync wrapper that drives an async coroutine.
    LangChain's ReAct agent invokes tools via `tool.invoke()`
    (sync) when the host graph node is sync. MCP tools are
    async-only, so we install a `func` that runs the coroutine
    on whatever event loop is available.

    Three cases:
      1. Called from a worker thread w/ no loop → asyncio.run.
      2. Called from inside a running loop (LangGraph async
         dispatch) → use anyio.from_thread.run if we're in a
         worker thread; else create a nested loop via
         nest_asyncio. Falls back to asyncio.run if neither
         works — which on Python 3.12+ raises a clear error
         the caller can act on.
    """
    def _sync(**kwargs):
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is None:
            return asyncio.run(coro_fn(**kwargs))
        # Inside a running loop: try nest_asyncio (well-supported
        # but adds a dep; falls back to a fresh thread+loop).
        try:
            import nest_asyncio
            nest_asyncio.apply(running)
            return running.run_until_complete(coro_fn(**kwargs))
        except ImportError:
            import threading
            import queue
            q: queue.Queue = queue.Queue(maxsize=1)

            def _runner():
                try:
                    q.put(("ok", asyncio.run(coro_fn(**kwargs))))
                except Exception as e:
                    q.put(("err", e))
            t = threading.Thread(target=_runner, daemon=True)
            t.start()
            t.join()
            kind, val = q.get()
            if kind == "err":
                raise val
            return val
    return _sync

# Underscore-aware word boundary: `\b` treats `_` as a word char so
# `\bcreate\b` does NOT match `create_issue`. We rebuild boundaries
# from `[^A-Za-z]` so snake_case / camelCase / dashed names all
# segment cleanly.
_MUTATE_RE = re.compile(
    r"(?:^|[^A-Za-z])"
    r"(create|update|delete|remove|write|set|push|merge|"
    r"kill|drop|reset|patch|move|rename|insert|destroy|"
    r"modify|put|post|run|exec|apply|publish|send|edit|append)"
    r"(?=[^A-Za-z]|$)",
    re.IGNORECASE)


def prefix_name(server: str, tool_name: str) -> str:
    return f"{server}__{tool_name}"


def prefix(tool: BaseTool, server: str) -> BaseTool:
    """Return a copy of `tool` whose name is `<server>__<original>`.
    Underlying coroutine / args_schema preserved. If the tool is
    async-only (langchain-mcp-adapters builds them with `coroutine=`
    only), also install a sync `func` bridge so LangChain's sync
    dispatcher (`tool.invoke()`) works — without this, ReAct agents
    running in sync graph nodes hit NotImplementedError on every MCP
    call."""
    upd: dict = {"name": prefix_name(server, tool.name)}
    if tool.coroutine is not None and tool.func is None:
        upd["func"] = _bridge_sync(tool.coroutine)
    return tool.model_copy(update=upd)


def looks_mutating(tool: BaseTool) -> bool:
    """Heuristic: name contains a mutate-y verb."""
    return bool(_MUTATE_RE.search(tool.name))


def wrap_confirm(tool: BaseTool, server: str,
                 confirm: Callable[[str, str], bool]) -> BaseTool:
    """Replace the tool's invocation with a confirm-gated wrapper.
    The wrapper builds a short args preview, asks `confirm(name,
    preview)`, and either short-circuits with a 'Skipped by user'
    message or delegates to the original tool unchanged."""
    orig_co = tool.coroutine
    orig_fn = tool.func
    label = f"{server}/{tool.name}"

    def _preview(kwargs: dict) -> str:
        bits = []
        for k, v in kwargs.items():
            s = repr(v)
            bits.append(f"{k}={s[:80] + '…' if len(s) > 80 else s}")
        return ", ".join(bits)

    async def _async_wrapper(**kwargs):
        if not confirm(label, _preview(kwargs)):
            return (f"Skipped by user: {label} not invoked "
                    f"(args: {_preview(kwargs)}).")
        if orig_co is not None:
            return await orig_co(**kwargs)
        return orig_fn(**kwargs)

    def _sync_wrapper(**kwargs):
        if not confirm(label, _preview(kwargs)):
            return (f"Skipped by user: {label} not invoked "
                    f"(args: {_preview(kwargs)}).")
        return orig_fn(**kwargs)

    return tool.model_copy(update={
        "coroutine": _async_wrapper if orig_co is not None else None,
        "func": _sync_wrapper if orig_co is None else orig_fn,
    })


def filter_allowed(tools: list[BaseTool],
                   allowed: tuple[str, ...],
                   denied: tuple[str, ...]
                   ) -> list[BaseTool]:
    """Per-server filter against UNPREFIXED tool names (config is
    written using the server's native names)."""
    out: list[BaseTool] = []
    for t in tools:
        if denied and t.name in denied:
            continue
        if allowed and t.name not in allowed:
            continue
        out.append(t)
    return out
