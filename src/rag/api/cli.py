"""
Terminal chat client for the LangGraph agent — Claude-Code-style UX:
rounded input box, ⏺ tool bullets with ⎿ result previews, transient
spinner, markdown-rendered answers, slash commands.

Entry: `cto` (via [project.scripts]) or `make chat`.
"""

import argparse
import itertools
import json
import os
import re
import sys
import time
import uuid

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style as PTStyle
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

console = Console()

ACCENT = "#d97757"          # claude-code orange
DIM = "grey50"
RESULT = "grey62"
BORDER = "grey39"

_SPIN = ["✻", "✢", "·", "✢"]
_THINK = ["Thinking", "Searching", "Reasoning", "Tracing", "Wandering",
          "Pondering", "Scheming", "Mulling"]

_SLASH = ["/new", "/sources", "/verbose", "/clear", "/help", "/exit",
          "/sessions", "/trace", "/notrace", "/skills", "/jobs",
          "/superdev", "/exit-superdev", "/playbook", "/playbooks"]


def _skill_slash_completions() -> list[str]:
    """Lazy: only loads the registry when local mode imports it."""
    try:
        from ..skills import list_skills
        return [f"/{s.name}" for s in list_skills()]
    except Exception:
        return []

_FOOTNOTE_BLOCK_RE = re.compile(r"(?:\n\s*\[SOURCE_\d+\]\s*:[^\n]*)+\s*$")
_INLINE_SOURCE_RE = re.compile(r"\[?SOURCE_(\d+)\]?")
# @-attachments: @./rel.png, @/abs/path.log, @~/file.txt — only paths
# that exist on disk are extracted (so '@user' in prose is left alone).
_ATTACH_RE = re.compile(r"(?<!\S)@((?:~|\.{0,2})?/[^\s]+|[\w.-]+\.\w{1,6})")


def _extract_attachments(line: str) -> tuple[str, list[str]]:
    """Pull @path tokens out of the input line. Returns
    (line_with_tokens_removed, [existing_file_paths]). Tokens that
    don't resolve to a real file are left in the text untouched."""
    paths: list[str] = []
    def _take(m):
        p = os.path.expanduser(m.group(1))
        if os.path.isfile(p):
            paths.append(p)
            return ""
        return m.group(0)
    rest = _ATTACH_RE.sub(_take, line)
    return " ".join(rest.split()), paths


# ── small renderers ─────────────────────────────────────────────────

def _bullet(label: str, body: str = "", *, style: str = "white",
            child: int | None = None) -> Text:
    t = Text()
    if child is not None:
        t.append("  ", style="")
        t.append(f"[{child}] ", style="bold magenta")
    t.append("⏺ ", style=f"bold {ACCENT}")
    t.append(label, style=f"bold {style}")
    if body:
        t.append(f" {body}", style=DIM)
    return t


def _sub(body: str, *, ok: bool = True,
         child: int | None = None) -> Text:
    t = Text()
    if child is not None:
        t.append("  ", style="")
        t.append(f"[{child}] ", style="magenta")
    t.append("  ⎿  ", style=DIM)
    t.append(body, style=RESULT if ok else "red")
    return t


def _todo_panel(items: list[dict], done: int, total: int) -> Panel:
    rows: list[Text] = []
    for i, it in enumerate(items, 1):
        mark, style = (("✓", DIM) if it.get("done")
                       else ("·", "white"))
        t = Text()
        t.append(f"  {mark} ", style=ACCENT if not it.get("done")
                 else DIM)
        t.append(f"{i}. ", style=DIM)
        t.append(str(it.get("text", "")), style=style)
        rows.append(t)
    return Panel(
        Group(*(rows or [Text("  (empty)", style=DIM)])),
        title=f"☑ {done}/{total}",
        title_align="left", border_style=BORDER,
        padding=(0, 1), expand=False)


_NODE_LABELS = {
    "reset_turn": "starting", "input_guard": "checking guards",
    "load_memories": "loading memories", "query_analysis": "analyzing query",
    "clarify": "resolving scope", "router": "routing",
    "simple_rag": "retrieving", "agent_loop": "reasoning",
    "deep_research": "fanning out", "evaluator": "self-grading",
    "output_guard": "verifying citations", "respond": "finalizing",
    "summarize": "compressing history", "save_memories": "saving",
}


class _Spinner:
    """Self-animating status line. Live's refresh thread calls
    __rich__() on every repaint, so frame + elapsed advance even when
    the graph emits nothing (e.g. during a slow LLM call). Mutate
    .label / .note / .n_tools / .above from the stream loop."""

    def __init__(self, label: str):
        self.t0 = time.perf_counter()
        self._frame = itertools.count()
        self.label = label
        self.note = ""
        self.n_tools = 0
        self.above = None  # optional renderable shown above the line

    def at(self, node: str | None):
        if node:
            self.note = _NODE_LABELS.get(node, node)

    def __rich__(self):
        f = next(self._frame)
        elapsed = time.perf_counter() - self.t0
        bits = []
        if self.note:
            bits.append(self.note)
        if self.n_tools:
            bits.append(f"{self.n_tools} tool"
                        f"{'s' if self.n_tools != 1 else ''}")
        bits.append(f"{elapsed:.1f}s")
        bits.append("ctrl+c to interrupt")
        line = Text()
        line.append(f"{_SPIN[f % len(_SPIN)]} ", style=f"bold {ACCENT}")
        line.append(f"{self.label}… ", style=ACCENT)
        line.append(f"({' · '.join(bits)})", style=DIM)
        if self.above is not None:
            return Group(self.above, Text(), line)
        return line


def _format_args(args: dict) -> str:
    parts = []
    for k, v in (args or {}).items():
        s = repr(v)
        if len(s) > 60:
            s = s[:57] + "…'"
        parts.append(f"{k}={s}")
    return ", ".join(parts)


def _answer_renderable(answer: str, citations: list[dict]):
    body = _FOOTNOTE_BLOCK_RE.sub("", (answer or "").rstrip())
    body = _INLINE_SOURCE_RE.sub(
        lambda m: f"[{m.group(1)}]", body)  # compact superscript-ish
    items = [Markdown(body or "_(no answer)_",
                      code_theme="ansi_dark", hyperlinks=True)]
    if citations:
        items.append(Text())
        items.append(Text("Citations", style=f"bold {DIM}"))
        for c in citations:
            n = c["ref"].split("_")[-1]
            loc = c.get("url") or c.get("file", "?")
            sym = f"  {c['symbol']}" if c.get("symbol") else ""
            t = Text()
            t.append(f"  [{n}] ", style=ACCENT)
            t.append(loc, style="cyan")
            t.append(sym, style=DIM)
            items.append(t)
    return Padding(Group(*items), (0, 0, 0, 2))


def _welcome(session_id: str, user_id: str) -> Panel:
    g = Group(
        Text.assemble(
            (" ✻ ", f"bold {ACCENT}"),
            ("Code", "bold"), (" / ", DIM),
            ("Trace", "bold"), (" / ", DIM),
            ("Origin", "bold"),
        ),
        Text(),
        Text("   From signal to source — cited to the line.",
             style="italic " + DIM),
        Text(),
        Text.assemble(
            ("   session: ", DIM), (session_id, "cyan"),
            ("   user: ", DIM), (user_id, "cyan"),
        ),
        Text("   /help for commands", style=DIM),
    )
    return Panel(g, border_style=ACCENT, padding=(1, 2), expand=False)


def _help_panel() -> Panel:
    rows = [
        ("/new [id]", "start or switch session"),
        ("/sources", "list indexed repos/docs/confluence"),
        ("/sessions", "list recent sessions"),
        ("/verbose [mode]", "terse | normal | detailed"),
        ("/trace", "last Phoenix trace URL"),
        ("/notrace", "toggle per-request tracing on/off"),
        ("/clear", "clear screen"),
        ("/exit  ctrl+d", "quit"),
        ("q: …  /  detail: …", "inline verbosity prefix"),
        ("@path/to/file", "attach image/text file (OCR/read into query)"),
        ("/skills", "list available skills"),
        ("/jobs", "list scheduled jobs + last/next fire"),
        ("/<skill> [repo]", "run a skill (e.g. /onboarding-tour acme-api)"),
        ("/superdev [repo] [playbook…]", "⚡ enter host-coding mode (local only)"),
        ("/superdev allow <dir>", "extend write-jail to <dir>"),
        ("/playbook <name>", "load a methodology playbook (superdev only)"),
        ("/playbooks", "list available playbooks"),
        ("/exit-superdev", "leave superdev → PR/discard/keep"),
    ]
    from rich.table import Table
    tbl = Table.grid(padding=(0, 3))
    tbl.add_column(style="bold cyan", no_wrap=True)
    tbl.add_column(style=DIM)
    for cmd, desc in rows:
        tbl.add_row(Text(cmd), Text(desc))
    return Panel(tbl, title="commands", border_style=BORDER,
                 padding=(1, 2), expand=False)


# ── remote (thin HTTP/SSE client) ───────────────────────────────────

def _auth_headers(api_key: str | None) -> dict:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _iter_sse(resp):
    """Minimal SSE parser over an httpx streaming response."""
    event, data = None, []
    for raw in resp.iter_lines():
        if not raw:
            if data:
                yield event or "message", "\n".join(data)
            event, data = None, []
            continue
        if raw.startswith(":"):
            continue
        field, _, value = raw.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    if data:
        yield event or "message", "\n".join(data)


def _run_turn_remote(remote: str, user_msg: str, *, session_id: str,
                     user_id: str, verbosity: str, pending: dict | None,
                     use_cache: bool, api_key: str | None,
                     trace: bool = True,
                     ) -> tuple[dict | None, str | None, str]:
    """One turn over POST /query (SSE). Returns
    (new_pending, trace_url, session_id) — server may assign session."""
    import httpx

    body = {"session_id": session_id, "user_id": user_id,
            "verbosity": verbosity, "stream": True,
            "use_cache": use_cache, "trace": trace}
    if pending:
        opts = pending.get("options") or []
        ans = user_msg.strip()
        if ans.isdigit() and opts and 1 <= int(ans) <= len(opts):
            ans = opts[int(ans) - 1]
        body["resume"] = ans
    else:
        body["q"] = user_msg

    spin = _Spinner(_THINK[hash(user_msg) % len(_THINK)])
    spin.note = "connecting"
    resolved_q = user_msg
    resolved_scope: list[str] = []
    new_pending: dict | None = None
    trace_url: str | None = None
    final: dict = {}

    try:
        with Live(spin, console=console, refresh_per_second=10,
                  transient=True) as live, \
             httpx.stream("POST", f"{remote.rstrip('/')}/query",
                          json=body, timeout=300.0,
                          headers=_auth_headers(api_key)) as resp:
            if resp.status_code == 401:
                live.stop()
                console.print(_sub(
                    "401 Unauthorized — set CTO_API_KEY or pass "
                    "--api-key", ok=False))
                return None, None, session_id
            resp.raise_for_status()
            spin.note = "waiting"
            for ev, data in _iter_sse(resp):
                try:
                    d = json.loads(data)
                except Exception:
                    continue
                if ev == "session":
                    session_id = d.get("session_id", session_id)
                elif ev == "cache":
                    live.console.print(_bullet(
                        "cache",
                        f"(sim={d.get('score', 0):.2f}, "
                        f"q={d.get('cached_query')!r})",
                        style="yellow"))
                elif ev == "interrupt":
                    new_pending = d
                elif ev == "skill":
                    k = d.get("kind")
                    if k == "start":
                        spin.note = f"skill:{d.get('skill')}"
                        live.console.print(_bullet(
                            "skill",
                            f"{d.get('skill')} [{d.get('mode')}] "
                            f"repo={d.get('repo') or '—'}",
                            style="cyan"))
                    elif k == "tool_call":
                        spin.n_tools += 1
                        spin.note = f"calling {d.get('name')}"
                        live.console.print(_bullet(
                            d.get("name") or "?",
                            f"({_format_args(d.get('args'))})"))
                    elif k == "tool_result":
                        live.console.print(_sub(
                            f"{(d.get('preview') or '')[:100]}…"))
                elif ev == "node":
                    node = d.get("node")
                    spin.at(node)
                    if node == "query_analysis":
                        resolved_scope = d.get("repo_scope") or []
                        live.console.print(_bullet(
                            "analysis",
                            f"intent={d.get('intent','?')} "
                            f"scope={resolved_scope or 'all'}"
                            + (" needs_human"
                               if d.get("needs_human") else "")))
                    elif node == "input_guard" and d.get("resolved_query"):
                        resolved_q = d["resolved_query"]
                    elif node == "router":
                        live.console.print(_bullet(
                            "router", f"→ {d.get('route')}"))
                    elif node == "load_memories" and d.get("n_memories"):
                        live.console.print(_bullet(
                            "memories", f"loaded {d['n_memories']}"))
                    elif node == "clarify":
                        if d.get("resolved_query"):
                            resolved_q = d["resolved_query"]
                        if d.get("repo_scope"):
                            resolved_scope = d["repo_scope"]
                        live.console.print(_bullet(
                            "clarified",
                            f"scope={resolved_scope or '—'}"))
                    elif node == "deep_research":
                        live.console.print(_bullet(
                            "deep_research",
                            f"{d.get('sub_questions',0)} sub-q → "
                            f"{d.get('findings',0)} branches"))
                    elif node == "evaluator":
                        ea = d.get("eval") or {}
                        if ea:
                            fp = ea.get("fast_path")
                            tag = (f" ({fp})" if fp else "")
                            live.console.print(_sub(
                                f"eval: grounded={ea.get('groundedness')} "
                                f"complete={ea.get('completeness')}{tag}"))
                        if d.get("refine_hint"):
                            live.console.print(_bullet(
                                "retry",
                                f"#{d.get('eval_iter',0)} — "
                                f"{d['refine_hint'][:80]}",
                                style="yellow"))
                    for tc in d.get("tool_calls") or []:
                        spin.n_tools += 1
                        live.console.print(_bullet(
                            tc["name"], f"({_format_args(tc.get('args'))})"))
                    for tr in d.get("tool_results") or []:
                        preview = (tr.get("preview") or "") \
                            .replace("\n", " ")[:100]
                        live.console.print(_sub(f"{preview}…"))
                elif ev in ("done", "paused"):
                    final = d
                    trace_url = d.get("trace_url")
    except KeyboardInterrupt:
        console.print(_sub("interrupted", ok=False))
        return None, None, session_id
    except Exception as e:
        console.print(_sub(f"{type(e).__name__}: {e}", ok=False))
        return None, None, session_id

    if new_pending:
        q = new_pending.get("question", "Need clarification")
        opts = new_pending.get("options") or []
        rows = [Text(q, style="bold")]
        for i, o in enumerate(opts, 1):
            rows.append(Text.assemble((f"  {i}. ", ACCENT), (o, "cyan")))
        rows.append(Text())
        rows.append(Text("Reply with a number or free text.", style=DIM))
        console.print(Panel(Group(*rows), title="❓ clarify",
                            border_style="yellow", padding=(1, 2)))
        return new_pending, None, session_id

    elapsed = time.perf_counter() - spin.t0
    answer = final.get("answer", "")
    citations = final.get("citations") or []

    console.print(Padding(Rule(style=DIM, characters="·"), (0, 0, 0, 2)))
    if resolved_q != user_msg or resolved_scope:
        console.print(Padding(Text.assemble(
            ("🔎 ", ""), (resolved_q, "italic"),
            ("   📦 ", ""),
            (", ".join(resolved_scope) if resolved_scope else "all repos",
             DIM)), (0, 0, 0, 2)))
    console.print()
    console.print(_answer_renderable(answer, citations))
    console.print()
    console.print(Text.assemble(
        ("  ", ""), (f"⏱ {elapsed:.1f}s", DIM),
        (f" · {spin.n_tools} tool call{'s' if spin.n_tools != 1 else ''}"
         if spin.n_tools else "", DIM),
        (f" · route={final.get('route','?')}", DIM)))
    if trace_url:
        console.print(Text.assemble(
            ("  🔍 ", DIM), (trace_url, f"link {DIM}")))
    console.print()
    return None, trace_url, session_id


def _cmd_sources_remote(remote: str, api_key: str | None):
    import httpx
    try:
        r = httpx.get(f"{remote.rstrip('/')}/sources", timeout=10,
                      headers=_auth_headers(api_key))
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        console.print(_sub(f"unavailable: {e}", ok=False))
        return
    lines = [Text("Repos", style="bold")]
    for r in d.get("repos") or []:
        lines.append(Text.assemble(
            ("  ⎿  ", DIM), (r["repo"], "cyan"),
            (f"  {r['chunks']} chunks", DIM)))
    docs = [x for x in (d.get("docs") or [])
            if not x["filepath"].startswith("confluence/")]
    conf = [x for x in (d.get("docs") or [])
            if x["filepath"].startswith("confluence/")]
    lines += [Text(), Text.assemble(
        ("Local docs  ", "bold"),
        (f"{len(docs)} file(s), {sum(x['chunks'] for x in docs)} chunks"
         if docs else "(none)", DIM))]
    lines += [Text(), Text.assemble(
        ("Confluence  ", "bold"),
        (f"{len(conf)} page(s)" if conf else "(none)", DIM))]
    console.print(Panel(Group(*lines), title="📚 sources",
                        border_style=BORDER, padding=(1, 2)))


def _cmd_jobs(remote: str | None, api_key: str | None):
    rows: list[dict] = []
    if remote:
        import httpx
        try:
            r = httpx.get(f"{remote.rstrip('/')}/jobs", timeout=10,
                          headers=_auth_headers(api_key))
            r.raise_for_status()
            rows = r.json().get("jobs", [])
        except Exception as e:
            console.print(_sub(f"unavailable: {e}", ok=False))
            return
    else:
        try:
            from datetime import datetime, timezone
            from ..scheduler import load_jobs
            from ..scheduler.store import last_run_at, last_run
            now = datetime.now(timezone.utc)
            for j in load_jobs():
                last = last_run_at(j.name)
                lr = last_run(j.name) or {}
                rows.append({
                    "name": j.name, "kind": j.kind,
                    "schedule": j.cron or f"every {j.interval_min}m",
                    "enabled": j.enabled,
                    "last_run": last.strftime("%m-%d %H:%M")
                                if last else "—",
                    "status": lr.get("status") or "—",
                    "next_fire": j.next_fire(now, last)
                                  .strftime("%m-%d %H:%M"),
                })
        except Exception as e:
            console.print(_sub(f"unavailable: {e}", ok=False))
            return
    if not rows:
        console.print(Panel(
            Text("(no jobs — create data/schedules.yaml)",
                 style=DIM),
            title="jobs", border_style=BORDER, padding=(1, 2)))
        return
    lines = []
    for j in rows:
        on = "" if j.get("enabled", True) else "  (disabled)"
        st = j.get("status", "—")
        st_style = {"ok": "green", "error": "red",
                    "running": "yellow"}.get(st, DIM)
        lines.append(Text.assemble(
            ("  ", ""), (j["name"], "bold cyan"),
            (f"  [{j['kind']}]", DIM),
            (f"  {j['schedule']}", DIM), (on, "red")))
        lines.append(Text.assemble(
            ("    last ", DIM), (str(j.get("last_run", "—")), ""),
            (" ", ""), (st, st_style),
            ("   next ", DIM), (str(j.get("next_fire", "—")), "")))
    console.print(Panel(Group(*lines), title="⏰ scheduled jobs",
                        border_style=BORDER, padding=(1, 2)))


def _cmd_skills(remote: str | None):
    rows = []
    if remote:
        import httpx
        try:
            r = httpx.get(f"{remote.rstrip('/')}/skills", timeout=10)
            r.raise_for_status()
            rows = r.json().get("skills", [])
        except Exception as e:
            console.print(_sub(f"unavailable: {e}", ok=False))
            return
    else:
        try:
            from ..skills import list_skills
            rows = [{"name": s.name, "description": s.description,
                     "mode": s.sandbox.mode,
                     "tools": len(s.tools_allowed)}
                    for s in list_skills()]
        except Exception as e:
            console.print(_sub(f"unavailable: {e}", ok=False))
            return
        # Phase 8.5-E: union in generic SKILLS.md skills (superdev-only,
        # tagged with their discovery source). Custom-format wins on
        # name collision (back-compat).
        try:
            from ..skills.generic import discover as _disc_generic
            existing = {r["name"] for r in rows}
            for g in _disc_generic().values():
                if g.name in existing:
                    continue
                rows.append({
                    "name": g.name,
                    "description": g.description,
                    "mode": f"generic:{g.source}",
                    "tools": len(g.allowed_tools),
                })
        except Exception:
            pass
    if not rows:
        console.print(Panel(
            Text("(none — drop a .md file into data/skills/)",
                 style=DIM),
            title="skills", border_style=BORDER, padding=(1, 2)))
        return
    lines = []
    for s in rows:
        lines.append(Text.assemble(
            ("  /", DIM), (s["name"], "bold cyan"),
            (f"  [{s.get('mode','—')}]", DIM)))
        if s.get("description"):
            lines.append(Text(f"    {s['description']}", style=DIM))
    console.print(Panel(Group(*lines), title="skills",
                        border_style=BORDER, padding=(1, 2)))


def _cmd_sessions_remote(remote: str, api_key: str | None):
    import httpx
    try:
        r = httpx.get(f"{remote.rstrip('/')}/sessions", timeout=10,
                      headers=_auth_headers(api_key))
        r.raise_for_status()
        rows = r.json().get("sessions", [])
    except Exception as e:
        console.print(_sub(f"unavailable: {e}", ok=False))
        return
    lines = [Text.assemble(("  ⎿  ", DIM), (r["thread_id"], "cyan"),
                           (f"  {r['checkpoints']} checkpoints", DIM))
             for r in rows]
    console.print(Panel(Group(*(lines or [Text("  (none)", style=DIM)])),
                        title="sessions", border_style=BORDER,
                        padding=(1, 2)))


# ── slash command handlers ──────────────────────────────────────────

def _cmd_sources():
    from ..ingest.incremental import list_repos
    from ..ingest.docs import list_docs
    repos = list_repos()
    docs = [d for d in list_docs()
            if not d["filepath"].startswith("confluence/")]
    lines = [Text("Repos", style="bold")]
    for r in repos or []:
        lines.append(Text.assemble(
            ("  ⎿  ", DIM), (r["repo"], "cyan"),
            (f"  {r['chunks']} chunks", DIM)))
    if not repos:
        lines.append(Text("  ⎿  (none)", style=DIM))
    lines.append(Text())
    lines.append(Text.assemble(
        ("Local docs  ", "bold"),
        (f"{len(docs)} file(s), {sum(d['chunks'] for d in docs)} chunks"
         if docs else "(none)", DIM)))
    try:
        from ..connectors.confluence import sync_status
        rows = sync_status()
        lines.append(Text())
        lines.append(Text("Confluence", style="bold"))
        for r in rows or []:
            lines.append(Text.assemble(
                ("  ⎿  ", DIM), (r["space_key"], "cyan"),
                (f"  {r['pages_indexed']} pages", DIM)))
        if not rows:
            lines.append(Text("  ⎿  (not configured)", style=DIM))
    except Exception:
        pass
    console.print(Panel(Group(*lines), title="📚 sources",
                        border_style=BORDER, padding=(1, 2)))


def _cmd_sessions():
    try:
        from ..indexing.graph_db import get_conn
        with get_conn().cursor() as cur:
            cur.execute(
                "SELECT thread_id, count(*) AS n FROM checkpoints "
                "GROUP BY thread_id ORDER BY max(checkpoint_id) DESC LIMIT 20")
            rows = cur.fetchall()
    except Exception as e:
        console.print(_sub(f"unavailable: {e}", ok=False))
        return
    lines = [Text.assemble(("  ⎿  ", DIM), (r["thread_id"], "cyan"),
                           (f"  {r['n']} checkpoints", DIM)) for r in rows]
    console.print(Panel(Group(*(lines or [Text("  (none)", style=DIM)])),
                        title="sessions", border_style=BORDER,
                        padding=(1, 2)))


# ── one turn ────────────────────────────────────────────────────────

def _run_turn(graph, user_msg: str, *, session_id: str, user_id: str,
              verbosity: str, pending: dict | None,
              use_cache: bool,
              trace: bool = True,
              mode: str = "regular") -> tuple[dict | None, str | None]:
    """Drive one streamed turn. Returns (new_pending_interrupt, trace_url)."""
    from langchain_core.messages import (
        HumanMessage, AIMessage, ToolMessage, AIMessageChunk,
    )
    from ..agents.interrupt_helpers import extract_interrupt, resume_command
    from ..memory.cache import (
        cache_lookup, cache_store, store_eligible, seed_history_on_hit,
    )
    from ..tracing import trace_config, push_scores

    config, tracer = trace_config(
        session_id, user_id=user_id, query=user_msg,
        tags=["cli", f"verbosity:{verbosity}"], suppress=not trace)

    spin = _Spinner(_THINK[hash(user_msg) % len(_THINK)])
    streaming = ""
    answer = ""
    citations: list[dict] = []
    resolved_q = user_msg
    resolved_scope: list[str] = []
    seen_calls: set[str] = set()
    new_pending: dict | None = None

    with Live(spin, console=console, refresh_per_second=10,
              transient=True) as live, tracer:
        # Resume path: this message answers a clarify interrupt.
        if pending:
            opts = pending.get("options") or []
            ans = user_msg.strip()
            if ans.isdigit() and opts and 1 <= int(ans) <= len(opts):
                ans = opts[int(ans) - 1]
            payload = resume_command(ans)
        else:
            from ..agents.nodes.intro import is_intro_query
            if (use_cache and verbosity in ("normal", "terse")
                    and not is_intro_query(user_msg)):
                spin.note = "checking cache"
                try:
                    hit = cache_lookup(user_msg, threshold=0.93,
                                       mode=mode)
                except Exception:
                    hit = None
                if hit:
                    seed_history_on_hit(graph, config, user_msg, hit)
                    live.stop()
                    console.print(_bullet(
                        "cache",
                        f"(sim={hit['score']:.2f}, "
                        f"{hit['age_sec']:.0f}s ago, "
                        f"q={hit['cached_query']!r})",
                        style="yellow"))
                    console.print()
                    console.print(_answer_renderable(
                        hit["answer"], hit.get("citations") or []))
                    console.print()
                    return None, None
            spin.note = "starting"
            payload = {
                "query": user_msg, "user_id": user_id,
                "verbosity": verbosity,
                "messages": [HumanMessage(content=user_msg)],
            }

        try:
            for ev in graph.stream(payload, config=config,
                                   stream_mode=["messages", "updates",
                                                "custom"]):
                if (ireq := extract_interrupt(ev)):
                    new_pending = ireq
                    break

                # NOTE: don't shadow the function param `mode`
                # (used by cache_store post-loop) — keep the loop var
                # named `kind`.
                kind, data = ev
                if kind == "messages":
                    chunk, meta = data
                    spin.at(meta.get("langgraph_node"))
                    if meta.get("langgraph_node") not in (
                            "agent_loop", "simple_rag", "deep_research"):
                        continue
                    if isinstance(chunk, AIMessageChunk):
                        for tcc in (chunk.tool_call_chunks or []):
                            name = tcc.get("name")
                            tcid = tcc.get("id") or name
                            if name and tcid not in seen_calls:
                                seen_calls.add(tcid)
                                spin.note = f"calling {name}"
                                streaming = ""
                                spin.above = None
                        if chunk.content:
                            streaming += chunk.content
                            spin.note = "streaming"
                            spin.above = Padding(
                                Markdown(streaming,
                                         code_theme="ansi_dark"),
                                (0, 0, 0, 2))

                elif kind == "custom":
                    se = (data or {}).get("skill_event")
                    if not se:
                        continue
                    k = se.get("kind")
                    ch = se.get("child")
                    if k == "spawn":
                        spin.note = f"spawn[{ch}]"
                        live.console.print(_bullet(
                            f"spawn[{ch}]",
                            f"({se.get('mode')}) "
                            f"{se.get('task','')!r}",
                            style="magenta"))
                        continue
                    if k == "spawn_parallel":
                        spin.note = f"spawning {se.get('n')}"
                        live.console.print(_bullet(
                            "spawn_parallel",
                            f"{se.get('n')} children…",
                            style="magenta"))
                        continue
                    if k == "spawn_done":
                        st = se.get("status", "?")
                        ok = st == "ok"
                        live.console.print(_sub(
                            f"{st} · {se.get('n_tools',0)} "
                            f"tools — "
                            f"{se.get('summary','')[:90]}",
                            ok=ok, child=ch))
                        continue
                    if k == "spawn_merge":
                        st = se.get("status")
                        live.console.print(_sub(
                            (f"merged {se.get('commits',0)} "
                             f"commit(s)" if st == "ok"
                             else f"CONFLICT — kept at "
                                  f"{se.get('path')}"),
                            ok=(st == "ok"), child=ch))
                        continue
                    if k == "start":
                        spin.note = f"skill:{se.get('skill')}"
                        live.console.print(_bullet(
                            "skill",
                            f"{se.get('skill')} "
                            f"[{se.get('mode')}] "
                            f"repo={se.get('repo') or '—'}"
                            + (f" cid={se['cid'][:14]}…"
                               if se.get('cid') else ""),
                            style="cyan"))
                    elif k == "tool_call":
                        spin.n_tools += 1
                        spin.note = (f"[{ch}] {se.get('name')}"
                                     if ch is not None
                                     else f"calling {se.get('name')}")
                        if ch is None:
                            streaming = ""
                            spin.above = None
                        live.console.print(_bullet(
                            se.get("name") or "?",
                            f"({_format_args(se.get('args'))})",
                            child=ch))
                    elif k == "tool_result":
                        if se.get("name") == "todo":
                            continue
                        live.console.print(_sub(
                            f"{(se.get('preview') or '')[:100]}…",
                            child=ch))
                        # Clear stale streaming counter so next
                        # thinking round starts fresh.
                        streaming = ""
                        spin.above = None
                    elif k == "text":
                        # Don't render intermediate thinking as a
                        # Markdown overlay — superdev's agent often
                        # echoes code/doc snippets while reasoning,
                        # which flashes large blocks on screen until
                        # the next tool_call clears them. Track length
                        # only; final answer renders post-loop from
                        # graph state.
                        streaming += se.get("delta") or ""
                        spin.note = f"streaming ({len(streaming)} chars)"
                        spin.above = None
                    elif k == "todo":
                        d, t = se.get("done", 0), se.get("total", 0)
                        spin.note = f"☑ {d}/{t}"
                        live.console.print(Padding(
                            _todo_panel(se.get("items") or [],
                                        d, t), (0, 0, 0, 2)))
                    elif k == "end":
                        spin.note = "finalizing"
                    continue

                elif kind == "updates":
                    for node, delta in data.items():
                        if not isinstance(delta, dict):
                            continue
                        spin.at(node)
                        if node == "input_guard" and delta.get("query"):
                            resolved_q = delta["query"]
                        elif node == "query_analysis":
                            qp = delta.get("query_plan") or {}
                            resolved_scope = list(qp.get("repo_scope") or [])
                            live.console.print(_bullet(
                                "analysis",
                                f"intent={qp.get('intent','?')} "
                                f"scope={resolved_scope or 'all'}"
                                + (" needs_human"
                                   if qp.get("needs_human") else "")))
                        elif node == "router":
                            live.console.print(_bullet(
                                "router", f"→ {delta.get('route')}"))
                        elif node == "load_memories":
                            n = len(delta.get("user_memories") or [])
                            if n:
                                live.console.print(_bullet(
                                    "memories", f"loaded {n}"))
                        elif node == "clarify" and delta.get("clarified"):
                            qp = delta.get("query_plan") or {}
                            if delta.get("query"):
                                resolved_q = delta["query"]
                            if qp.get("repo_scope"):
                                resolved_scope = list(qp["repo_scope"])
                            live.console.print(_bullet(
                                "clarified",
                                f"scope={resolved_scope or '—'}"))
                        elif node == "deep_research":
                            sq = len(delta.get("sub_questions") or [])
                            fd = len(delta.get("findings") or [])
                            live.console.print(_bullet(
                                "deep_research",
                                f"{sq} sub-q → {fd} branches"))
                        elif node == "evaluator":
                            es = delta.get("eval_scores") or {}
                            ans = (es.get("answer") or {})
                            if ans:
                                fp = ans.get("fast_path")
                                tag = (f" ({fp})" if fp else "")
                                live.console.print(_sub(
                                    f"eval: grounded="
                                    f"{ans.get('groundedness')} "
                                    f"complete={ans.get('completeness')}"
                                    f"{tag}"))
                            if delta.get("refine_hint"):
                                live.console.print(_bullet(
                                    "retry",
                                    f"#{delta.get('eval_iter', 0)} — "
                                    f"{delta['refine_hint'][:80]}",
                                    style="yellow"))

                        # skill_runner / superdev_loop already
                        # streamed their tool calls live via custom
                        # skill_events; don't re-render them from
                        # the bulk delta (causes duplicate trace).
                        if node not in ("skill_runner",
                                        "superdev_loop"):
                            for m in delta.get("messages", []):
                                if isinstance(m, AIMessage) and m.tool_calls:
                                    for tc in m.tool_calls:
                                        tcid = tc.get("id") or tc["name"]
                                        args = _format_args(tc["args"])
                                        spin.n_tools += 1
                                        live.console.print(_bullet(
                                            tc["name"], f"({args})"))
                                        seen_calls.add(tcid)
                                elif isinstance(m, ToolMessage):
                                    preview = (str(m.content)
                                               .replace("\n", " ")[:100])
                                    live.console.print(_sub(
                                        f"{preview}…"))
                        if delta.get("answer"):
                            answer = delta["answer"]
                            streaming = ""
                            spin.above = None
                        if "citations" in delta:
                            citations = delta["citations"] or []
        except KeyboardInterrupt:
            live.stop()
            console.print(_sub("interrupted", ok=False))
            return None, None

    # ── post-stream ────────────────────────────────────────────────
    if new_pending:
        q = new_pending.get("question", "Need clarification")
        opts = new_pending.get("options") or []
        body = [Text(q, style="bold")]
        for i, o in enumerate(opts, 1):
            body.append(Text.assemble((f"  {i}. ", ACCENT), (o, "cyan")))
        body.append(Text())
        body.append(Text("Reply with a number or free text.", style=DIM))
        console.print(Panel(Group(*body), title="❓ clarify",
                            border_style="yellow", padding=(1, 2)))
        return new_pending, None

    elapsed = time.perf_counter() - spin.t0
    try:
        final = graph.get_state(config).values
    except Exception:
        final = {"answer": answer, "citations": citations}
    answer = final.get("answer", answer)
    citations = final.get("citations") or citations

    console.print(Padding(Rule(style=DIM, characters="·"), (0, 0, 0, 2)))
    # Resolution banner (only when something was rewritten/scoped).
    if resolved_q != user_msg or resolved_scope:
        console.print(Padding(Text.assemble(
            ("🔎 ", ""), (resolved_q, "italic"),
            ("   📦 ", ""),
            (", ".join(resolved_scope) if resolved_scope else "all repos",
             DIM),
        ), (0, 0, 0, 2)))
    console.print()
    console.print(_answer_renderable(answer, citations))
    console.print()
    console.print(Text.assemble(
        ("  ", ""),
        (f"⏱ {elapsed:.1f}s", DIM),
        (f" · {spin.n_tools} tool call{'s' if spin.n_tools != 1 else ''}"
         if spin.n_tools else "", DIM),
        (f" · route={final.get('route','?')}", DIM),
    ))

    key, ok, why = store_eligible(user_msg, final, verbosity,
                                  was_resume=bool(pending))
    if use_cache and ok and answer:
        try:
            cache_store(key, answer, citations, mode=mode)
        except Exception:
            pass
    try:
        push_scores(tracer, final)
    except Exception:
        pass
    url = tracer.url()
    if url:
        console.print(Text.assemble(
            ("  🔍 ", DIM), (url, f"link {DIM}")))
    console.print()
    return None, url


# ── REPL ────────────────────────────────────────────────────────────

def _make_prompt_session() -> PromptSession:
    hist = os.path.expanduser("~/.cto_history")
    style = PTStyle.from_dict({
        "prompt": f"{ACCENT} bold",
        "bottom-toolbar": "bg:#1c1c1c #8a8a8a",
        "placeholder": "#5f5f5f italic",
    })
    words = list(_SLASH) + _skill_slash_completions()
    completer = WordCompleter(words, sentence=True, match_middle=False)
    return PromptSession(history=FileHistory(hist), style=style,
                          completer=completer, complete_while_typing=False)


def main():
    # Phase 10 subcommand routing — `cto setup`, `cto doctor`,
    # `cto compose <…>` short-circuit before the chat-REPL argparse
    # so they work without any of the heavy graph / tracing imports
    # below. `cto chat ...` and `cto [question]` continue to fall
    # through.
    argv = list(sys.argv[1:])
    if argv and argv[0] in ("setup", "doctor", "compose"):
        sub = argv.pop(0)
        if sub == "setup":
            from ..cli.setup import run as _setup_run
            sys.exit(_setup_run(force="--force" in argv))
        if sub == "doctor":
            from ..cli.doctor import run as _doc_run
            sys.exit(_doc_run(unsafe="--unsafe" in argv))
        if sub == "compose":
            from ..cli.compose import dispatch as _compose_dispatch
            sys.exit(_compose_dispatch(argv))
    if argv and argv[0] == "chat":
        argv.pop(0)  # `cto chat ...` == `cto ...` for the chat parser

    parser = argparse.ArgumentParser(prog="cto",
        description="Code/Trace/Origin — terminal chat (subcommands: "
                    "setup, doctor; default: chat)")
    parser.add_argument("question", nargs="?", help="one-shot question")
    parser.add_argument("--session", "-s", default=None)
    parser.add_argument("--user", "-u", default=os.environ.get("USER", "anon"))
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--verbosity", "-v", default="normal",
                        choices=["terse", "normal", "detailed"])
    parser.add_argument("--remote", "-r",
                        default=os.environ.get("CTO_REMOTE"),
                        help="Server URL (e.g. http://ec2-host:8000). "
                             "Thin SSE client; no local deps needed.")
    parser.add_argument("--api-key",
                        default=os.environ.get("CTO_API_KEY"),
                        help="Bearer key for --remote (or set CTO_API_KEY)")
    parser.add_argument("--no-trace", action="store_true",
                        help="suppress Phoenix tracing per-request")
    args = parser.parse_args(argv)

    remote = (args.remote or "").rstrip("/") or None
    api_key = args.api_key
    trace = not args.no_trace
    graph = None
    if not remote:
        os.environ.setdefault("UI_ENABLED", "false")
        from ..agents.graph import get_app
        from ..tracing import init_tracing
        init_tracing(quiet=True)
        graph = get_app()
        # FastAPI lifespan only fires for `make serve`. For `make chat`
        # we need to drive MCP startup ourselves so the superdev
        # toolset includes MCP tools / resources / prompts. Fail-soft:
        # a broken .mcp.json doesn't sink the CLI.
        try:
            import asyncio as _asyncio
            from ..mcp.client import connect_all as _mcp_connect
            _asyncio.run(_mcp_connect())
        except Exception as e:
            print(f"[mcp] init skipped: {type(e).__name__}: {e}")

    session_id = args.session or f"cli-{uuid.uuid4().hex[:8]}"
    user_id = args.user
    verbosity = args.verbosity
    use_cache = not args.no_cache
    pending: dict | None = None
    last_trace: str | None = None

    # ── superdev session state (Phase 8-C) ──────────────────────────
    # `sd` holds the active worktree + its compiled graph; None when
    # in the read-only graph. Mode switching is ONLY via user-typed
    # /superdev — never by router/regex/LLM.
    sd: dict | None = None
    sd_session = f"{session_id}/superdev"

    def _rebuild_sd_graph():
        from ..agents.superdev_graph import build_superdev_graph
        sd["graph"] = build_superdev_graph(
            sd["wt"], sd["roots"], cwd=sd["cwd"],
            playbooks=sd["playbooks"], todo_state=sd["todos"],
            session_tid=sd_session)

    def _enter_superdev(arg_str: str) -> bool:
        nonlocal sd
        from ..config import CTO_SUPERDEV_ENABLED
        if remote:
            console.print(_sub(
                "/superdev is local-only (refused with --remote). "
                "Run `cto` directly on your machine.", ok=False))
            return False
        if not CTO_SUPERDEV_ENABLED:
            console.print(_sub(
                "CTO_SUPERDEV_ENABLED=false — set it in your env "
                "to enable host-coding mode.", ok=False))
            return False
        from ..agents.playbooks import list_playbooks
        from pathlib import Path as _P
        # Parse "/superdev [repo] [playbook playbook …]" — first
        # token is the repo unless it matches a playbook name.
        toks = arg_str.split()
        avail_pb = set(list_playbooks())
        repo_arg = ""
        if toks and toks[0] not in avail_pb:
            repo_arg = toks.pop(0)
        playbooks = [t for t in toks if t in avail_pb]
        bad_pb = [t for t in toks if t not in avail_pb]
        if bad_pb:
            console.print(_sub(
                f"unknown playbook(s): {', '.join(bad_pb)} "
                f"(have: {', '.join(sorted(avail_pb)) or 'none'})",
                ok=False))
        try:
            if repo_arg:
                from ..agents.tools._repo import resolve_repo
                from ..vcs.worktree import create as wt_create
                repo, err = resolve_repo(repo_arg)
                if err or not repo:
                    console.print(_sub(err or "repo not found",
                                       ok=False))
                    return False
                wt = wt_create(repo, prefix="superdev",
                               session=session_id)
                roots = [wt.path]
                sd = {"wt": wt, "cwd": wt.path, "roots": roots,
                      "playbooks": playbooks, "todos": []}
                tools_note = (
                    "Host shell/write/edit/glob/patch, docker_run, "
                    "commit, create_pr + RAG (search_code, "
                    "find_callers, git_log, …). Writes jailed to "
                    "the worktree.")
                meta = [("cwd      ", str(wt.path)),
                        ("branch   ", wt.branch),
                        ("repo     ", repo)]
            else:
                cwd = _P.cwd().resolve()
                roots = [cwd]
                sd = {"wt": None, "cwd": cwd, "roots": roots,
                      "playbooks": playbooks, "todos": []}
                tools_note = (
                    "Shell-only: host_shell/read, docker_run, "
                    "ask_user, todo + RAG. No write/edit/commit/"
                    "create_pr — re-enter with `/superdev <repo>` "
                    "for code changes.")
                meta = [("cwd      ", str(cwd)),
                        ("mode     ", "shell-only (no worktree)")]
            _rebuild_sd_graph()
        except Exception as e:
            console.print(_sub(f"{type(e).__name__}: {e}", ok=False))
            sd = None
            return False
        if playbooks:
            meta.append(("playbooks", ", ".join(playbooks)))
        rows = [("⚡ SUPERDEV", "bold red"), ("\n\n", "")]
        for k, v in meta:
            rows += [(k, DIM), (v, "cyan"), ("\n", "")]
        rows += [("\n", ""),
                 (f"{tools_note} /exit-superdev to leave.",
                  "italic " + DIM)]
        console.print(Panel(Text.assemble(*rows),
                            border_style="red", padding=(1, 2)))
        return True

    def _exit_superdev():
        nonlocal sd, pending
        if not sd:
            console.print(_sub("not in superdev mode", ok=False))
            return
        wt = sd["wt"]
        if wt is None:
            sd = None
            pending = None
            console.print(_bullet("exited", "shell-only mode"))
            console.print()
            return
        from ..vcs.worktree import discard as wt_discard
        dirty = wt.has_changes() or wt.has_commits()
        if dirty:
            console.print(Panel(Text.assemble(
                ("Uncommitted/un-PR'd changes on ", ""),
                (wt.branch, "cyan"), (".\n", ""),
                ("  [k]eep   — keep worktree & branch on disk\n", DIM),
                ("  [d]iscard— delete worktree + branch\n", DIM),
                ("  [p]r     — commit-all + open a PR\n", DIM)),
                border_style="yellow", title="exit-superdev"))
            try:
                ans = input("  k/d/p › ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "k"
            if ans.startswith("d"):
                wt_discard(wt)
                console.print(_bullet("discarded", str(wt.path)))
            elif ans.startswith("p"):
                from ..vcs import pr as _pr
                try:
                    if wt.has_changes():
                        _pr.commit_all(wt, "chore", "superdev",
                                       "checkpoint before PR")
                    url = _pr.create_pr(
                        wt, title=f"superdev: {wt.repo}",
                        body="Opened on /exit-superdev.")
                    console.print(_bullet("PR", url, style="green"))
                except Exception as e:
                    console.print(_sub(f"PR failed: {e}", ok=False))
                    console.print(_bullet("kept", str(wt.path)))
            else:
                console.print(_bullet("kept", str(wt.path)))
        else:
            wt_discard(wt)
            console.print(_bullet("discarded",
                                  f"{wt.path} (no changes)"))
        sd = None
        pending = None
        console.print()

    def turn(msg: str):
        nonlocal pending, last_trace, session_id
        # @path attachments (image → OCR/vision; text → raw, capped).
        # Read client-side so --remote works without server file access.
        if not pending:
            stripped, paths = _extract_attachments(msg)
            if paths:
                from ..agents.vision import read_attachments
                attach_md, meta = read_attachments(paths)
                for m in meta:
                    tag = {"ocr": "OCR", "vision": "vision",
                           "file": "text", "none": "unread"}[m["method"]]
                    err = f" — {m['error']}" if m.get("error") else ""
                    console.print(_bullet(
                        "attach", f"📎 {m['name']} ({tag}, "
                        f"{m['chars']} chars){err}", style="cyan"))
                msg = attach_md + (
                    stripped or
                    "Describe and analyze the attached file(s).")
        if remote:
            pending, url, session_id = _run_turn_remote(
                remote, msg, session_id=session_id, user_id=user_id,
                verbosity=verbosity, pending=pending,
                use_cache=use_cache, api_key=api_key, trace=trace)
        elif sd:
            pending, url = _run_turn(
                sd["graph"], msg, session_id=sd_session,
                user_id=user_id, verbosity=verbosity,
                pending=pending, use_cache=use_cache, trace=trace,
                mode="superdev")
        else:
            pending, url = _run_turn(
                graph, msg, session_id=session_id, user_id=user_id,
                verbosity=verbosity, pending=pending,
                use_cache=use_cache, trace=trace)
        if url:
            last_trace = url

    if args.question:
        console.print()
        turn(args.question)
        return

    console.print()
    console.print(_welcome(session_id, user_id))
    if remote:
        console.print(Text.assemble(("  ⛭ remote: ", DIM),
                                     (remote, "cyan")))
    console.print()

    psession = _make_prompt_session()

    def toolbar():
        if sd:
            wt = sd["wt"]
            tail = (f"branch={wt.branch}" if wt
                    else "shell-only · no write/PR")
            pb = (f"  ·  📖 {','.join(sd['playbooks'])}"
                  if sd.get("playbooks") else "")
            td = sd.get("todos") or []
            tdc = (f"  ·  ☑ {sum(1 for t in td if t['done'])}"
                   f"/{len(td)}" if td else "")
            return [("class:bottom-toolbar",
                     f" ⚡ SUPERDEV  ·  cwd={sd['cwd']}  ·  "
                     f"{tail}{pb}{tdc}  ·  /exit-superdev ")]
        flag = {"terse": "⚡", "normal": "≡", "detailed": "📖"}[verbosity]
        p = "  ❓ awaiting clarification" if pending else ""
        return (f" {session_id}  ·  {flag} {verbosity}  ·  "
                f"cache {'on' if use_cache else 'off'}  ·  "
                f"trace {'on' if trace else 'OFF'}{p} ")

    while True:
        try:
            mark = "│ " if pending else ("⚡› " if sd else "› ")
            ph = ("answer the question above…" if pending
                  else "host-coding — /exit-superdev to leave"
                  if sd else "ask anything — /help for commands")
            line = psession.prompt(
                [("class:prompt", mark)],
                bottom_toolbar=toolbar,
                placeholder=[("class:placeholder", ph)],
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print(Text("\n  bye.\n", style=DIM))
            return
        if not line:
            continue

        # ── slash commands ─────────────────────────────────────────
        if line.startswith("/") and not pending:
            cmd, *rest = line.split(maxsplit=1)
            arg = rest[0] if rest else ""
            if cmd in ("/exit", "/quit"):
                return
            if cmd == "/clear":
                console.clear()
                continue
            if cmd == "/help":
                console.print(_help_panel())
                continue
            if cmd == "/new":
                if sd:
                    console.print(_sub(
                        "in superdev — /exit-superdev first",
                        ok=False))
                    continue
                session_id = arg or f"cli-{uuid.uuid4().hex[:8]}"
                pending = None
                console.print(_bullet("session", f"→ {session_id}"))
                console.print()
                continue
            if cmd == "/verbose":
                if arg in ("terse", "normal", "detailed"):
                    verbosity = arg
                else:
                    order = ["terse", "normal", "detailed"]
                    verbosity = order[(order.index(verbosity) + 1) % 3]
                console.print(_bullet("verbosity", f"→ {verbosity}"))
                console.print()
                continue
            if cmd == "/sources":
                (_cmd_sources_remote(remote, api_key) if remote
                 else _cmd_sources())
                continue
            if cmd == "/sessions":
                (_cmd_sessions_remote(remote, api_key) if remote
                 else _cmd_sessions())
                continue
            if cmd == "/notrace":
                trace = not trace
                console.print(_bullet(
                    "tracing", "→ ON" if trace else "→ OFF (suppressed)"))
                console.print()
                continue
            if cmd == "/trace":
                if last_trace:
                    console.print(Text(f"  {last_trace}",
                                       style=f"link {DIM}"))
                else:
                    console.print(_sub("no trace yet", ok=False))
                continue
            if cmd == "/skills":
                _cmd_skills(remote)
                continue
            if cmd == "/jobs":
                _cmd_jobs(remote, api_key)
                continue
            if cmd == "/superdev":
                sub = arg.split(maxsplit=1)
                if sd and sub and sub[0] == "allow":
                    d = os.path.expanduser(
                        sub[1] if len(sub) > 1 else "")
                    if d and os.path.isdir(d):
                        from pathlib import Path as _P
                        sd["roots"].append(_P(d).resolve())
                        console.print(_bullet(
                            "allow", f"writes now permitted in {d}"))
                    else:
                        console.print(_sub(
                            f"not a directory: {d!r}", ok=False))
                elif sd:
                    console.print(_sub(
                        "already in superdev — /exit-superdev first",
                        ok=False))
                else:
                    _enter_superdev(arg.strip())
                continue
            if cmd == "/exit-superdev":
                _exit_superdev()
                continue
            if cmd == "/playbooks":
                from ..agents.playbooks import list_playbooks
                pbs = list_playbooks()
                lines = ([Text.assemble(("  ", ""),
                                         (p, "cyan")) for p in pbs]
                         or [Text("  (none — drop a .md into "
                                  "data/playbooks/)", style=DIM)])
                console.print(Panel(Group(*lines),
                    title="📖 playbooks", border_style=BORDER,
                    padding=(1, 2)))
                continue
            if cmd == "/playbook":
                if not sd:
                    console.print(_sub(
                        "/playbook only works inside /superdev",
                        ok=False))
                    continue
                from ..agents.playbooks import list_playbooks
                names = arg.split()
                avail = set(list_playbooks())
                if not names:
                    cur = sd.get("playbooks") or []
                    console.print(_bullet(
                        "playbooks",
                        ", ".join(cur) if cur else "(none active)"))
                    continue
                if names == ["off"] or names == ["none"]:
                    sd["playbooks"] = []
                else:
                    bad = [n for n in names if n not in avail]
                    if bad:
                        console.print(_sub(
                            f"unknown: {', '.join(bad)} "
                            f"(have: {', '.join(sorted(avail))})",
                            ok=False))
                        continue
                    for n in names:
                        if n not in sd["playbooks"]:
                            sd["playbooks"].append(n)
                try:
                    _rebuild_sd_graph()
                    console.print(_bullet(
                        "playbooks",
                        f"→ {', '.join(sd['playbooks']) or '(none)'}"))
                except Exception as e:
                    console.print(_sub(f"{e}", ok=False))
                continue
            # /<skill-name> [repo] → run the skill. The router's
            # trigger regex would also catch natural-language
            # phrasings, but the slash form is explicit + scoped.
            sk_name = cmd.lstrip("/")
            if not remote and not sd:
                from ..skills import get as _get_skill
                if _get_skill(sk_name):
                    repo_arg = arg.split()[0] if arg else ""
                    line = (f"run skill {sk_name}"
                            + (f" on {repo_arg}" if repo_arg else ""))
                    console.print()
                    console.print(Rule(style=BORDER))
                    console.print()
                    console.print(_bullet("skill",
                                          f"{sk_name} {repo_arg}".strip(),
                                          style="cyan"))
                    turn(line)
                    continue
            # Generic SKILLS.md (Phase 8.5-E): inside /superdev,
            # `/<name>` resolving to a generic skill passes through
            # to the graph so superdev_loop's maybe_splice composes
            # the rendered body. Outside superdev, ignore — the
            # read-only graph doesn't consume generic skills.
            if sd and not remote:
                from ..skills.generic import get as _get_generic
                if _get_generic(sk_name):
                    console.print()
                    console.print(Rule(style=BORDER))
                    console.print()
                    console.print(_bullet(
                        "skill", f"/{sk_name} {arg}".strip(),
                        style="cyan"))
                    turn(line)
                    continue
            console.print(_sub(f"unknown command: {cmd}", ok=False))
            continue

        console.print()
        console.print(Rule(style=BORDER))
        console.print()
        turn(line)


if __name__ == "__main__":
    main()
