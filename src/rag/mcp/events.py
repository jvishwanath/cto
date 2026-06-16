"""
Runtime MCP event handlers — progress + server-side logging.

Registered at client construction via langchain-mcp-adapters
`Callbacks`. Two design constraints:

  * Best-effort: callback failures must NOT propagate up into the
    MCP SDK's notification dispatcher (would silently drop later
    notifications). Wrap every body in try/except.
  * Lightweight: the callback runs inside the MCP transport's async
    task chain. Avoid blocking work.

Output paths:

  * Project logger (`rag.mcp.events`) — always. Tail visible via
    `make docker-logs` / standard logging config.
  * LangGraph stream writer when in scope — emits a `skill_event`
    so the CLI / UI render the event live. Reached via
    `langgraph.config.get_stream_writer()`; raises when called
    outside a graph node, which we catch.

Cancellation propagation: handled automatically by the MCP SDK when
the calling coroutine receives `asyncio.CancelledError` — no
explicit handler needed here. See docs/MCP.md.

List-changed handling (`notifications/tools/list_changed`,
`notifications/resources/list_changed`) is deferred — tool /
resource caches go stale until restart. See docs/MCP.md "v1
non-scope".
"""

from __future__ import annotations

import logging

from langgraph.config import get_stream_writer

log = logging.getLogger(__name__)


def _emit_skill_event(payload: dict) -> None:
    """Best-effort emit a skill_event into the active LangGraph
    stream. No-op when called outside a graph node (or when the
    writer body itself raises)."""
    try:
        writer = get_stream_writer()
    except Exception:
        return
    try:
        writer({"skill_event": payload})
    except Exception:
        pass


async def on_progress(progress, total, message, context) -> None:
    """Fired when an MCP server emits `notifications/progress`
    during a tool call. We log + best-effort surface as a
    `skill_event` so the CLI shows the running %."""
    try:
        pct = None
        if total is not None and total > 0:
            try:
                pct = max(0.0, min(1.0, float(progress) / float(total)))
            except (TypeError, ValueError):
                pct = None
        msg = str(message) if message is not None else ""
        srv = getattr(context, "server_name", "?")
        tool = getattr(context, "tool_name", "?")
        log.info("[mcp progress] %s/%s %s/%s %s",
                 srv, tool, progress, total, msg)
        _emit_skill_event({
            "kind": "mcp_progress",
            "server": srv, "tool": tool,
            "progress": progress, "total": total,
            "pct": pct, "message": msg,
        })
    except Exception:
        log.debug("[mcp] on_progress handler crashed", exc_info=True)


async def on_logging_message(params, context) -> None:
    """Fired when an MCP server emits a logging message. Route the
    server's `level` to the matching Python log level."""
    try:
        srv = getattr(context, "server_name", "?")
        level = (getattr(params, "level", "info") or "info").lower()
        data = getattr(params, "data", "")
        logger_name = getattr(params, "logger", "") or srv
        py_level = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "notice": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "critical": logging.CRITICAL,
            "alert": logging.CRITICAL,
            "emergency": logging.CRITICAL,
        }.get(level, logging.INFO)
        log.log(py_level, "[mcp:%s] %s: %s",
                srv, logger_name, data)
        _emit_skill_event({
            "kind": "mcp_log",
            "server": srv, "level": level,
            "logger": logger_name,
            "message": str(data)[:500],
        })
    except Exception:
        log.debug("[mcp] on_logging_message handler crashed",
                  exc_info=True)


def build_callbacks():
    """Return the langchain-mcp-adapters Callbacks object configured
    with our handlers. None when the dependency isn't importable
    (defensive — main install requires it but tests may stub)."""
    try:
        from langchain_mcp_adapters.callbacks import Callbacks
    except ImportError:
        return None
    return Callbacks(
        on_progress=on_progress,
        on_logging_message=on_logging_message,
    )
