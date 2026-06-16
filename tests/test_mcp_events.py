"""
MCP runtime event handler tests.

Covers:
  * on_progress logs + emits skill_event with pct calculation,
    handles missing total / non-numeric values gracefully.
  * on_logging_message maps MCP level → Python log level,
    emits skill_event with capped message length.
  * Handler bodies are exception-safe — a broken stream writer
    must NOT propagate (would silently drop subsequent
    notifications from the MCP SDK dispatcher).
  * build_callbacks returns a Callbacks object with both handlers
    wired.
"""

import asyncio
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("LITELLM_API_KEY", "sk-test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

import conftest  # noqa: F401

from smoke_test import check, section, results  # noqa: E402


def _ctx(server="srv", tool="t"):
    return SimpleNamespace(server_name=server, tool_name=tool)


# ── on_progress ─────────────────────────────────────────────────────

def test_on_progress():
    section("on_progress")
    from rag.mcp import events

    captured: list[dict] = []

    def fake_writer(payload):
        captured.append(payload)

    @check("pct = progress/total, emitted as skill_event")
    def _():
        captured.clear()
        with patch("rag.mcp.events.get_stream_writer",
                   return_value=fake_writer, create=True):
            asyncio.run(events.on_progress(
                progress=25, total=100,
                message="indexing", context=_ctx()))
        assert len(captured) == 1, captured
        ev = captured[0]["skill_event"]
        assert ev["kind"] == "mcp_progress"
        assert ev["pct"] == 0.25, ev
        assert ev["message"] == "indexing", ev

    @check("missing total → pct None, no exception")
    def _():
        captured.clear()
        with patch("rag.mcp.events.get_stream_writer",
                   return_value=fake_writer, create=True):
            asyncio.run(events.on_progress(
                progress=5, total=None,
                message=None, context=_ctx()))
        assert captured[0]["skill_event"]["pct"] is None

    @check("non-numeric progress → handler doesn't raise")
    def _():
        captured.clear()
        with patch("rag.mcp.events.get_stream_writer",
                   return_value=fake_writer, create=True):
            asyncio.run(events.on_progress(
                progress="bad", total=100,
                message="x", context=_ctx()))
        assert captured[0]["skill_event"]["pct"] is None

    @check("no active graph (writer raises) → no exception")
    def _():
        def boom():
            raise RuntimeError("no graph context")

        with patch("rag.mcp.events.get_stream_writer",
                   side_effect=boom, create=True):
            asyncio.run(events.on_progress(
                progress=1, total=10, message=None,
                context=_ctx()))

    @check("writer body throws → handler still returns cleanly")
    def _():
        def broken_writer(_):
            raise RuntimeError("broken")

        with patch("rag.mcp.events.get_stream_writer",
                   return_value=broken_writer, create=True):
            asyncio.run(events.on_progress(
                progress=1, total=10, message=None,
                context=_ctx()))


# ── on_logging_message ──────────────────────────────────────────────

def test_on_logging_message():
    section("on_logging_message")
    from rag.mcp import events

    captured: list[dict] = []

    def fake_writer(payload):
        captured.append(payload)

    @check("emits skill_event with server / level / message")
    def _():
        captured.clear()
        params = SimpleNamespace(
            level="warning", data="disk almost full", logger="db")
        with patch("rag.mcp.events.get_stream_writer",
                   return_value=fake_writer, create=True):
            asyncio.run(events.on_logging_message(
                params, _ctx(server="postgres")))
        ev = captured[0]["skill_event"]
        assert ev["kind"] == "mcp_log"
        assert ev["server"] == "postgres", ev
        assert ev["level"] == "warning", ev
        assert ev["message"] == "disk almost full", ev

    @check("unknown level → defaults to INFO, no raise")
    def _():
        captured.clear()
        params = SimpleNamespace(
            level="bogus", data="x", logger="")
        with patch("rag.mcp.events.get_stream_writer",
                   return_value=fake_writer, create=True):
            asyncio.run(events.on_logging_message(
                params, _ctx()))

    @check("long message truncated to 500 chars")
    def _():
        captured.clear()
        params = SimpleNamespace(
            level="info", data="A" * 2000, logger="")
        with patch("rag.mcp.events.get_stream_writer",
                   return_value=fake_writer, create=True):
            asyncio.run(events.on_logging_message(
                params, _ctx()))
        assert len(captured[0]["skill_event"]["message"]) == 500


# ── build_callbacks ─────────────────────────────────────────────────

def test_build_callbacks():
    section("build_callbacks")
    from rag.mcp import events

    @check("Callbacks object with both handlers wired")
    def _():
        cb = events.build_callbacks()
        assert cb is not None
        assert cb.on_progress is events.on_progress
        assert cb.on_logging_message is events.on_logging_message


def main() -> int:
    test_on_progress()
    test_on_logging_message()
    test_build_callbacks()
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
