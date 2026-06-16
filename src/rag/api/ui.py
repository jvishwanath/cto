"""
Gradio chat UI for the agentic RAG, mounted under FastAPI at /ui.
Streams LangGraph events (router → tool calls → answer → citations)
into the chatbot via a generator.
"""

import inspect
import time
import uuid

import gradio as gr
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, AIMessageChunk

from ..agents.graph import get_app
from ..agents.interrupt_helpers import extract_interrupt, resume_command
from ..ingest.incremental import list_repos
from ..ingest.docs import list_docs
from ..indexing.graph_db import get_conn
from ..memory.cache import (
    cache_lookup, cache_store, store_eligible as _store_eligible,
    seed_history_on_hit,
)
from ..tracing import trace_config, push_scores

_graph = get_app()

_FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?'
    'family=Space+Grotesk:wght@400;500;600&display=swap" rel="stylesheet">'
)

_CSS = """
/* ── App header above the chat ──────────────────────────────────── */
#app-header-row {
    align-items: center;
    border-bottom: 1px solid var(--border-color-primary, #e5e5e5);
    margin-bottom: 0.5rem;
    padding-bottom: 0.5rem;
}
#app-header {
    padding: 0.25rem 0 0.25rem 0;
}
#app-header h1 {
    margin: 0;
    font-family: 'Space Grotesk', var(--rag-font) !important;
    font-size: 1.75rem;
    font-weight: 500;
    letter-spacing: 0.02em;
    line-height: 1.1;
}
#app-header h1 .sep {
    color: var(--body-text-color-subdued, #6b7280);
    font-weight: 400;
    margin: 0 0.15em;
}
#app-header p {
    margin: 0.25rem 0 0 0;
    font-family: 'Space Grotesk', var(--rag-font) !important;
    font-size: 0.9rem;
    font-weight: 400;
    letter-spacing: 0.01em;
    color: var(--body-text-color-subdued, #6b7280);
}
#sources-btn { align-self: center; }

/* ── Sources modal overlay ─────────────────────────────────────────
   Gradio wraps the component in extra divs that may share the elem_id;
   target the OUTERMOST as a full-screen flex overlay (no transform —
   transform on an ancestor traps position:fixed of descendants), and
   style the inner card via a class on the Group's content. */
.gradio-container { overflow: visible !important; }
#sources-overlay {
    position: fixed !important;
    inset: 0 !important;
    z-index: 10000 !important;
    display: flex !important;
    align-items: center;
    justify-content: center;
    background: rgba(0,0,0,0.45);
    padding: 2rem;
}
#sources-overlay.hide, #sources-overlay.hidden { display: none !important; }
#sources-overlay > * {
    /* neutralize Gradio's column wrapper so it doesn't stretch */
    width: auto !important;
    flex: 0 0 auto !important;
}
.sources-card {
    width: min(640px, 90vw) !important;
    max-height: 80vh;
    overflow-y: auto;
    background: var(--background-fill-primary, #1f1f1f);
    border: 1px solid var(--border-color-primary, #3a3a3a);
    border-radius: 12px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.4);
    padding: 1rem 1.25rem !important;
}
.sources-card .row:first-child { align-items: center; margin-bottom: 0.5rem; }
#sources-body { font-size: 0.92rem; }
/* Jobs modal — wider than sources to fit the table. */
.jobs-card { width: min(960px, 95vw) !important; }
#jobs-table table { font-size: 0.85rem; }
#jobs-table th { white-space: nowrap; }
#jobs-log {
    font-family: var(--rag-mono); font-size: 0.78rem;
    max-height: 240px; overflow-y: auto;
    background: var(--background-fill-secondary);
    border-radius: 8px; padding: 8px 10px;
    white-space: pre;
}

/* ── Typography: one font, consistent sizes everywhere ───────────── */
:root {
    --rag-font: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter',
                Roboto, 'Helvetica Neue', Arial, sans-serif;
    --rag-mono: ui-monospace, 'SF Mono', 'JetBrains Mono', Menlo,
                Consolas, monospace;
    --rag-size: 15px;
    --rag-line: 1.6;
}
.gradio-container, .gradio-container * {
    font-family: var(--rag-font) !important;
}
.gradio-container {
    font-size: var(--rag-size) !important;
    line-height: var(--rag-line) !important;
}
/* Code stays monospace but matches body size */
.gradio-container code, .gradio-container pre,
.gradio-container kbd, .gradio-container samp {
    font-family: var(--rag-mono) !important;
    font-size: 0.93em !important;
}
/* Flatten heading sizes inside chat messages — agent answers use
   ##/### which by default render huge. Keep them bold but near body
   size so the message reads as a continuous document. */
#chatbot .message h1, #chatbot .message h2 {
    font-size: 1.15em !important;
    font-weight: 600 !important;
    margin: 0.8em 0 0.4em !important;
}
#chatbot .message h3, #chatbot .message h4,
#chatbot .message h5, #chatbot .message h6 {
    font-size: 1.0em !important;
    font-weight: 600 !important;
    margin: 0.7em 0 0.3em !important;
}
#chatbot .message p, #chatbot .message li,
#chatbot .message td, #chatbot .message th {
    font-size: 1.0em !important;
    line-height: var(--rag-line) !important;
}
#chatbot .message table {
    font-size: 0.95em !important;
    display: block; max-width: 100%; overflow-x: auto;
    border-collapse: separate; border-spacing: 0;
}
#chatbot .message table td, #chatbot .message table th {
    white-space: nowrap; padding: 6px 12px;
    border-bottom: 1px solid var(--border-color-primary);
}
/* Sticky first column so row labels stay visible while scrolling
   wide comparison tables horizontally. */
#chatbot .message table tr > :first-child {
    position: sticky; left: 0; z-index: 1;
    background: var(--background-fill-secondary);
    border-right: 1px solid var(--border-color-primary);
    font-weight: 600;
}
#chatbot .message table thead th {
    position: sticky; top: 0; z-index: 2;
    background: var(--background-fill-secondary);
    font-weight: 600;
}
#chatbot .message table thead tr > :first-child {z-index: 3;}
/* Let the bubble use the full chat width when it contains a wide
   table — comparison tables need more than 980px. */
#chatbot .bot.message:has(table) {
    max-width: 100% !important;
    width: 100% !important;
}
#chatbot .message hr {margin: 1em 0 !important;}
/* Sidebar typography — same scale as chat */
.sidebar h1, .sidebar h2 {font-size: 1.15em !important; font-weight: 600 !important;}
.sidebar h3 {font-size: 1.0em !important; font-weight: 600 !important;}
.sidebar, .sidebar * {font-size: var(--rag-size) !important;}
.sidebar code {font-size: 0.9em !important;}
/* Buttons / inputs match body size */
button, input, textarea, select {
    font-size: var(--rag-size) !important;
    font-family: var(--rag-font) !important;
}

footer {display: none !important;}
.gradio-container {max-width: 100% !important; padding: 0 !important;}

/* Full-height chat: make every ancestor of #chatbot a flex column that
   grows, so the chatbot can flex-grow to fill the viewport. Gradio's
   fill_height only applies to the top container — nested Row/Column
   need explicit help. */
/* With Sidebar + chatbot as direct Blocks children, fill_height does
   most of the work. Just ensure the chatbot's inner wrapper fills it. */
div#chatbot {min-height: 0 !important;}
div#chatbot > .wrapper, div#chatbot .bubble-wrap {height: 100% !important;}

/* Message layout — Gradio 6 structure:
   .message-wrap > .message-row.{user,bot}-row > .flex-wrap.role
     > .{user,bot}.message > .message.panel-full-width > [data-testid] > .message-content
   The .message-row shrinks to content by default; force it full-width
   so the bubble's max-width is relative to the chat panel. */
#chatbot .message-wrap {
    display: flex !important;
    flex-direction: column !important;
    gap: 12px !important;
}
#chatbot .message-row {
    width: 100% !important;
    max-width: 100% !important;
}
#chatbot .message-row .flex-wrap {
    width: 100% !important;
    max-width: 100% !important;
}
/* The OUTER bubble (.user.message / .bot.message) gets the visual style.
   Use min() so short messages don't stretch absurdly wide. */
#chatbot .user.message, #chatbot .bot.message {
    border: none !important;
    border-radius: 14px !important;
    padding: 12px 16px !important;
    box-shadow: 0 1px 2px rgba(0,0,0,.06);
    max-width: min(85%, 980px) !important;
    width: fit-content !important;
}
/* Also strip the outer chatbot container border so the chat area
   sits flush with the page. */
div#chatbot, div#chatbot > .wrapper {
    border: none !important;
    box-shadow: none !important;
    background: transparent !important;
}
#chatbot .user-row {justify-content: flex-end !important;}
#chatbot .bot-row {justify-content: flex-start !important;}
#chatbot .user.message {
    background: var(--color-accent-soft) !important;
}
#chatbot .bot.message {
    background: var(--background-fill-secondary) !important;
}
/* Inner .message.panel-full-width should NOT add its own constraints */
#chatbot .message .message {
    max-width: 100% !important;
    padding: 0 !important;
    border-radius: 0 !important;
    box-shadow: none !important;
    background: transparent !important;
}
#chatbot .message-content {
    width: auto !important;
    min-width: 0;
}
/* Per-message copy/action buttons: by default Gradio renders them as a
   separate row BELOW the bubble (causing the awkward floating box +
   vertical gap). Anchor them to the bubble's top-right corner instead,
   visible on hover. */
#chatbot .user.message, #chatbot .bot.message {position: relative;}
#chatbot .message-buttons-right,
#chatbot .message-buttons-left,
#chatbot .message-buttons {
    position: absolute !important;
    top: 6px !important;
    right: 6px !important;
    left: auto !important;
    bottom: auto !important;
    margin: 0 !important;
    display: flex !important;
    gap: 4px;
    opacity: 0;
    transition: opacity .12s ease;
    background: var(--background-fill-primary);
    border-radius: 6px;
    padding: 2px;
    z-index: 2;
}
#chatbot .user.message:hover .message-buttons,
#chatbot .user.message:hover .message-buttons-right,
#chatbot .user.message:hover .message-buttons-left,
#chatbot .bot.message:hover .message-buttons,
#chatbot .bot.message:hover .message-buttons-right,
#chatbot .bot.message:hover .message-buttons-left {
    opacity: 1;
}
#chatbot .message-buttons button,
#chatbot .message-buttons-right button,
#chatbot .message-buttons-left button {
    padding: 4px !important;
    min-width: 0 !important;
}
#input-row {gap: 6px; flex: 0 0 auto !important; align-items: flex-end;}
#input-row button {min-width: 44px; padding: 0 12px;}
/* MultimodalTextbox grows when files are attached; cap and align. */
#msg-input {flex: 1 1 auto !important;}
#msg-input textarea {max-height: 160px;}
#msg-input .thumbnails {gap: 4px; padding: 4px 0;}
/* Wrapper that anchors the settings popover to the ⚙ button. */
#settings-anchor {
    position: relative !important;
    flex: 0 0 auto !important;
    width: auto !important; min-width: 0 !important;
}
#settings-anchor > button {min-width: 44px;}
/* The popover itself: absolutely positioned card that floats above
   the input row, right-aligned to the ⚙ button. */
.settings-pop {
    position: absolute !important;
    bottom: calc(100% + 8px) !important;
    right: 0 !important;
    z-index: 9999;
    width: 260px !important; min-width: 260px !important;
    background: var(--background-fill-primary, #1f1f1f);
    border: 1px solid var(--border-color-primary, #3a3a3a);
    border-radius: 10px;
    box-shadow: 0 12px 32px rgba(0,0,0,0.35);
    padding: 12px 14px !important;
    gap: 10px !important;
}
.settings-pop .gr-form, .settings-pop fieldset {
    border: none !important; padding: 0 !important; margin: 0 !important;
    background: transparent !important;
}
.settings-pop label > span {font-size: 0.85em !important;}
/* Verbosity radio inside the popover — segmented look. */
#verbosity-toggle .wrap {gap: 2px !important; flex-wrap: nowrap !important;}
#verbosity-toggle label {
    padding: 6px 8px !important; margin: 0 !important;
    border-radius: 6px; cursor: pointer; flex: 1; text-align: center;
}
#verbosity-toggle input {display: none;}
#verbosity-toggle label:has(input:checked) {
    background: var(--color-accent-soft);
}
.settings-hint {
    font-size: 0.78em !important;
    color: var(--body-text-color-subdued);
    margin: -4px 0 0 0 !important;
}
.thinking summary {
    cursor: pointer; user-select: none;
    padding: 6px 10px; border-radius: 8px;
    background: var(--background-fill-secondary);
    display: inline-block;
    color: var(--body-text-color-subdued);
}
.thinking summary:hover {background: var(--background-fill-primary);}
.thinking[open] summary {margin-bottom: 8px;}
.thinking pre {
    max-height: 320px; overflow: auto;
    border-radius: 8px;
}
/* Inline [SOURCE_N] markers in answer body — small superscript-like badge. */
#chatbot .message a[href^="#src-"], #chatbot .cite-ref {
    font-size: 0.72em !important;
    vertical-align: super;
    padding: 0 4px;
    border-radius: 4px;
    background: var(--background-fill-secondary);
    color: var(--body-text-color-subdued);
    text-decoration: none;
    font-family: var(--rag-mono);
}
/* Collapsed citations block */
.cite-list {margin-top: 0.8em;}
.cite-list summary {
    cursor: pointer; user-select: none;
    padding: 4px 8px; border-radius: 6px;
    display: inline-block;
    color: var(--body-text-color-subdued);
    background: var(--background-fill-secondary);
}
.cite-list[open] summary {margin-bottom: 6px;}
.cite-list .cite-rows {
    font-size: 0.92em; opacity: 0.85;
    max-height: 280px; overflow-y: auto;
    border-left: 2px solid var(--border-color-primary);
    padding: 4px 0 4px 10px; margin-top: 4px;
}
.cite-list .cite-rows > div {padding: 2px 0;}
.cite-list .cite-rows code {font-size: 0.88em;}
"""

_DARK_TOGGLE_JS = """
() => {
  const b = document.body;
  const dark = b.classList.toggle('dark');
  localStorage.setItem('rag-theme', dark ? 'dark' : 'light');
}
"""

_LOAD_THEME_JS = """
() => {
  if (localStorage.getItem('rag-theme') === 'dark') {
    document.body.classList.add('dark');
  }
}
"""


# ── Data helpers ────────────────────────────────────────────────────

def _list_sessions() -> list[str]:
    try:
        with get_conn().cursor() as cur:
            cur.execute(
                "SELECT thread_id FROM checkpoints "
                "GROUP BY thread_id ORDER BY max(checkpoint_id) DESC LIMIT 50"
            )
            return [r["thread_id"] for r in cur.fetchall()]
    except Exception:
        return []


def _ago(ts) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts.timestamp())
    if s < 120: return f"{s}s ago"
    if s < 7200: return f"{s//60}m ago"
    if s < 172800: return f"{s//3600}h ago"
    return f"{s//86400}d ago"


def _sources_md() -> str:
    lines = ["#### Repos"]
    repos = list_repos()
    if repos:
        lines += [f"- `{r['repo']}` — {r['chunks']} chunks" for r in repos]
    else:
        lines.append("_(none — drop a repo into `data/repos/`)_")

    # Local docs: summary only (count + total chunks), not per-file.
    docs = [d for d in list_docs()
            if not d["filepath"].startswith("confluence/")]
    lines.append("\n#### Local Docs")
    if docs:
        total = sum(d["chunks"] for d in docs)
        lines.append(f"- {len(docs)} file(s) — {total} chunks "
                     f"_(`data/docs/`)_")
    else:
        lines.append("_(none — drop a PDF/MD into `data/docs/`)_")

    # Confluence: one row per space with page/chunk counts + last sync.
    lines.append("\n#### Confluence")
    try:
        from ..connectors.confluence import sync_status, load_config
        rows = sync_status()
        cfg = load_config()
        base = (cfg.base_url if cfg else "").rstrip("/")
        if rows:
            for r in rows:
                space = r["space_key"]
                link = (f"[`{space}`]({base}/spaces/{space})"
                        if base else f"`{space}`")
                lines.append(
                    f"- {link} — {r['pages_indexed']} pages "
                    f"_(synced {_ago(r['last_run'])}; "
                    f"{r['pages_changed']}Δ "
                    f"{r['pages_deleted']}−)_"
                )
        elif cfg and cfg.spaces:
            lines.append(
                f"_(configured: {', '.join(s.key for s in cfg.spaces)} — "
                f"not synced yet; `make confluence-sync`)_"
            )
        else:
            lines.append("_(not configured — see "
                         "`data/connectors/confluence.yaml.example`)_")
    except Exception as e:
        lines.append(f"_(unavailable: {type(e).__name__})_")

    return "\n".join(lines)


def _jobs_data() -> tuple[list[list], list[str], str, str]:
    """(rows, job_names, log_tail, hint) for the Jobs modal."""
    try:
        from ..scheduler import status_rows, LOG_PATH
        rows = status_rows()
    except Exception as e:
        return ([], [], "",
                f"_(scheduler unavailable: {type(e).__name__}: {e})_")
    if not rows:
        return ([], [], "",
                "_(no jobs — create `data/schedules.yaml`)_")
    cols = [[r["name"], r["kind"], r["schedule"], r["repos"],
             r["last_run"],
             {"ok": "✓ ok", "error": "✗ error",
              "running": "⟳ running"}.get(r["status"], r["status"]),
             r["duration"], f"{r['next_fire']} (in {r['in']})",
             r["notify"]] for r in rows]
    tail = ""
    try:
        if LOG_PATH.exists():
            tail = "\n".join(
                LOG_PATH.read_text(encoding="utf-8")
                .splitlines()[-60:])
    except Exception:
        pass
    return cols, [r["name"] for r in rows], tail, ""


_JOBS_HEADERS = ["name", "kind", "schedule", "repos", "last run",
                 "status", "dur", "next fire", "notify"]


import re as _re

_FOOTNOTE_BLOCK_RE = _re.compile(r"(?:\n\s*\[SOURCE_\d+\]\s*:[^\n]*)+\s*$")
_INLINE_SOURCE_RE = _re.compile(r"\[?SOURCE_(\d+)\]?")


def _format_answer(body: str, citations: list[dict],
                   anchor_ns: str = "") -> str:
    """Shared rendering for an answer body + citations:
    strips the agent's trailing footnote block, turns inline
    [SOURCE_N] into small badges, and appends the collapsible
    citations <details>. Used by both the live render() and the
    cache-hit path so they look identical.

    anchor_ns namespaces the #src-N anchors per message — without it,
    every turn emits id="src-0", and an inline [SOURCE_0] link jumps
    to the FIRST turn's citation in the DOM (duplicate-id bug)."""
    pfx = f"src-{anchor_ns}-" if anchor_ns else "src-"
    parts: list[str] = []
    if body:
        body = _FOOTNOTE_BLOCK_RE.sub("", body.rstrip())
        body = _INLINE_SOURCE_RE.sub(
            lambda m: (f'<a class="cite-ref" href="#{pfx}{m.group(1)}">'
                       f'{m.group(1)}</a>'),
            body,
        )
        parts.append(body)
    if citations:
        def _row(c: dict) -> str:
            n = c["ref"].split("_")[-1]
            label = c["file"]
            if c.get("url"):
                title = c.get("title") or label
                label = (f'<a href="{c["url"]}" target="_blank" '
                         f'rel="noopener">{title}</a>')
            sym = (f' <code>{c["symbol"]}</code>'
                   if c.get("symbol") else "")
            return (f'<div id="{pfx}{n}"><code>[{c["ref"]}]</code> '
                    f'{label}{sym}</div>')
        rows = "".join(_row(c) for c in citations)
        parts.append(
            f'<details class="cite-list">'
            f'<summary>📎 {len(citations)} citation'
            f'{"s" if len(citations) != 1 else ""}</summary>'
            f'<div class="cite-rows">{rows}</div>'
            f'</details>'
        )
    return "\n\n".join(parts)


def _load_history(session_id: str) -> list[dict]:
    if not session_id:
        return []
    try:
        state = _graph.get_state({"configurable": {"thread_id": session_id}})
        msgs = state.values.get("messages", [])
    except Exception:
        return []
    out = []
    for m in msgs:
        if isinstance(m, HumanMessage):
            out.append({"role": "user", "content": m.content})
        elif isinstance(m, AIMessage) and m.content and not m.tool_calls:
            out.append({"role": "assistant", "content": m.content})
    return out


# ── Streaming generator ─────────────────────────────────────────────

_BLANK_INPUT = {"text": "", "files": []}

_METHOD_LABEL = {"ocr": "OCR", "vision": "vision", "file": "text",
                 "none": "unread"}


def _chat(user_msg: dict | str, history: list[dict], session_id: str,
          pending_interrupt: dict | None, verbosity: str,
          trace_on: bool):
    """
    Gradio generator: yields (history, _BLANK_INPUT, pending_interrupt) on
    each step. user_msg from MultimodalTextbox is {"text": str,
    "files": [paths]}; files are read (image→OCR/vision, text→raw,
    capped) and prepended to the query for THIS turn (ephemeral —
    not indexed; lives in checkpointer history only).
    """
    if isinstance(user_msg, dict):
        typed = (user_msg.get("text") or "").strip()
        files = list(user_msg.get("files") or [])
    else:  # back-compat: plain Textbox
        typed = (user_msg or "").strip()
        files = []

    attach_md = ""
    attach_note = ""
    if files:
        from ..agents.vision import read_attachments
        attach_md, meta = read_attachments(files)
        notes = [f"📎 {m['name']} — {_METHOD_LABEL.get(m['method'], '?')}"
                 for m in meta]
        attach_note = "  \n_" + " · ".join(notes) + "_"

    if not typed and not attach_md:
        yield history, _BLANK_INPUT, pending_interrupt
        return

    # Graph sees full extracted content; bubble shows typed text +
    # compact 📎 note(s) (not the raw dump).
    display_msg = (typed or "_(attachment only)_") + attach_note
    user_msg = attach_md + (typed or
                            "Describe and analyze the attached file(s).")

    history = list(history)
    # Per-turn anchor namespace: index of this assistant message in the
    # chat. Keeps #src-N anchors unique across turns (duplicate ids
    # otherwise send every citation link to turn 1).
    anchor_ns = str(len(history) + 1)
    history.append({"role": "user", "content": display_msg})
    history.append({"role": "assistant", "content": "_…thinking_"})
    yield history, _BLANK_INPUT, None

    config, tracer = trace_config(
        session_id, user_id=session_id, query=user_msg,
        tags=["ui", f"verbosity:{verbosity}"],
        suppress=not trace_on,
    )

    if pending_interrupt:
        # User's message is the resume answer.
        opts = pending_interrupt.get("options") or []
        ans = user_msg.strip()
        if ans.isdigit() and opts and 1 <= int(ans) <= len(opts):
            ans = opts[int(ans) - 1]
        inputs = resume_command(ans)
    else:
        # LOOKUP on the user's literal text every turn. STORE decision
        # is made AFTER the graph runs, keyed on the RESOLVED query
        # (post-clarify, post-redaction) — see _store_eligible() below.
        # Skip lookup for intro-style queries: the intro response is
        # dynamic (live repo inventory) and ~200ms anyway, and the
        # same phrasing answered via agent-route in a mid-history
        # session can poison the cache with a non-intro answer.
        from ..agents.nodes.intro import is_intro_query
        if verbosity in ("normal", "terse") and not is_intro_query(typed):
            try:
                hit = cache_lookup(user_msg, threshold=0.93)
                if not hit:
                    print(f"  [cache] MISS")
            except Exception as e:
                print(f"  [cache] lookup FAILED: {type(e).__name__}: {e}")
                hit = None
            if hit:
                seed_history_on_hit(_graph, config, user_msg, hit)
                history[-1]["content"] = (
                    f'<details class="thinking"><summary>⚡ cached '
                    f'(sim={hit["score"]:.2f}, {hit["age_sec"]:.0f}s ago)</summary>\n\n'
                    f'```\n▸ cache hit: {hit["cached_query"]!r}\n```\n</details>\n\n'
                    + _format_answer(hit["answer"], hit.get("citations") or [],
                                     anchor_ns=anchor_ns)
                )
                yield history, _BLANK_INPUT, None
                return
        # Per-session user_id so save_memories doesn't pile facts into a
        # single shared namespace across all UI users/sessions.
        inputs = {"query": user_msg, "user_id": session_id,
                  "verbosity": verbosity,
                  "messages": [HumanMessage(content=user_msg)]}

    trace_lines: list[str] = []
    answer = ""
    streaming_text = ""
    citations: list = []
    iteration = 0
    seen_tool_calls: set[str] = set()
    new_interrupt: dict | None = None
    resolved_query: str = user_msg
    resolved_scope: list[str] = []
    t0 = time.perf_counter()

    def render(done: bool = False) -> str:
        parts = []
        # Always-visible resolution banner: shows the rewritten query
        # and final repo scope so wrong resolutions are caught at a
        # glance, not buried in the collapsed trace.
        if resolved_query != user_msg or resolved_scope:
            scope_str = ", ".join(resolved_scope) if resolved_scope else "all repos"
            q_line = (f"> 🔎 **Resolved query:** `{resolved_query}`  \n"
                      if resolved_query != user_msg else "")
            parts.append(
                f"{q_line}> 📦 **Scope:** {scope_str}"
            )
        if trace_lines:
            elapsed = time.perf_counter() - t0
            steps = sum(1 for ln in trace_lines if ln.startswith("▸"))
            label = (f"🔍 Reasoned for {elapsed:.1f}s · {steps} steps"
                     if done else f"🔍 Thinking… {steps} steps · {elapsed:.1f}s")
            open_attr = "" if done else " open"
            parts.append(
                f'<details class="thinking"{open_attr}>'
                f"<summary>{label}</summary>\n\n"
                f"```\n" + "\n".join(trace_lines) + "\n```\n"
                f"</details>"
            )
        body = answer or streaming_text
        formatted = _format_answer(body, citations, anchor_ns=anchor_ns)
        if formatted:
            parts.append(formatted)
        return "\n\n".join(parts) if parts else "_…thinking_"

    def push(done=False):
        history[-1]["content"] = render(done)

    tracer.__enter__()
    try:
        for ev in _graph.stream(inputs, config=config,
                                 stream_mode=["messages", "updates",
                                              "custom"]):
            # Interrupt? (arrives as ('updates', {'__interrupt__': (...)}))
            if (ireq := extract_interrupt(ev)):
                new_interrupt = ireq
                opts = ireq.get("options") or []
                opt_md = "\n".join(f"{i+1}. `{o}`" for i, o in enumerate(opts))
                history[-1]["content"] = (
                    render() + f"\n\n**❓ {ireq.get('question','Need clarification')}**"
                    + (f"\n\n{opt_md}\n\n_Reply with the option (number or text)._" if opts
                       else "\n\n_Reply below to continue._")
                )
                yield history, _BLANK_INPUT, new_interrupt
                return

            # NOTE: keep loop var named `kind` so it doesn't shadow
            # a future `mode` parameter (parity with cli.py).
            kind, payload = ev
            if kind == "messages":
                chunk, meta = payload
                node = meta.get("langgraph_node", "")
                if node not in ("agent_loop", "simple_rag", "deep_research"):
                    continue
                if isinstance(chunk, AIMessageChunk):
                    had_tool = False
                    for tcc in (chunk.tool_call_chunks or []):
                        name = tcc.get("name")
                        tcid = tcc.get("id") or name
                        if name and tcid not in seen_tool_calls:
                            seen_tool_calls.add(tcid)
                            had_tool = True
                            trace_lines.append(f"▸ {name}(…)")
                            push(); yield history, _BLANK_INPUT, None
                    # When the model starts emitting tool calls, the
                    # preceding streamed text was intermediate reasoning
                    # ("Let me search for…") — discard it so it doesn't
                    # accumulate across iterations into the final answer.
                    if had_tool:
                        streaming_text = ""
                    if chunk.content:
                        streaming_text += chunk.content
                        push(); yield history, _BLANK_INPUT, None

            elif kind == "custom":
                se = (payload or {}).get("skill_event")
                if not se:
                    continue
                k = se.get("kind")
                ch = se.get("child")
                pfx = f"[{ch}] " if ch is not None else ""
                ind = "    " if ch is not None else ""
                if k == "start":
                    trace_lines.append(
                        f"▸ skill:{se.get('skill')} "
                        f"[{se.get('mode')}] repo={se.get('repo') or '—'}")
                elif k == "spawn":
                    trace_lines.append(
                        f"▸ {pfx}spawn ({se.get('mode')}) "
                        f"{se.get('task','')!r}")
                elif k == "spawn_parallel":
                    trace_lines.append(
                        f"▸ spawn_parallel {se.get('n')} children")
                elif k == "spawn_done":
                    trace_lines.append(
                        f"{ind}↳ {pfx}{se.get('status')} — "
                        f"{(se.get('summary') or '')[:80]}")
                elif k == "spawn_merge":
                    trace_lines.append(
                        f"{ind}↳ {pfx}merge: {se.get('status')}"
                        + (f" {se.get('commits')} commits"
                           if se.get('status') == 'ok'
                           else f" kept@{se.get('path')}"))
                elif k == "tool_call":
                    args = ", ".join(f"{a}={v!r}"[:40]
                                     for a, v in (se.get("args") or {}).items())
                    trace_lines.append(
                        f"{ind}▸ {pfx}{se.get('name')}({args})")
                elif k == "tool_result":
                    trace_lines.append(
                        f"{ind}  ↳ {pfx}"
                        f"{(se.get('preview') or '')[:100]}…")
                elif k == "todo":
                    d, t = se.get("done", 0), se.get("total", 0)
                    trace_lines.append(f"▸ ☑ {d}/{t}")
                    for i, it in enumerate(
                            se.get("items") or [], 1):
                        m = "✓" if it.get("done") else "·"
                        trace_lines.append(
                            f"    {m} {i}. {it.get('text','')}")
                push(); yield history, _BLANK_INPUT, None

            elif kind == "updates":
                for node, delta in payload.items():
                    if not isinstance(delta, dict):
                        continue
                    if node == "query_analysis":
                        qp = delta.get("query_plan") or {}
                        scope = qp.get("repo_scope") or []
                        resolved_scope = list(scope)
                        trace_lines.append(
                            f"▸ analysis: intent={qp.get('intent','?')} "
                            f"scope={scope or 'ALL'}"
                            + (" needs_human" if qp.get("needs_human") else "")
                        )
                    elif node == "input_guard" and delta.get("query"):
                        resolved_query = delta["query"]
                    elif node == "router":
                        trace_lines.append(f"▸ router → {delta.get('route')}")
                    elif node == "load_memories":
                        n = len(delta.get("user_memories") or [])
                        if n:
                            trace_lines.append(f"▸ loaded {n} user memories")
                    elif node == "clarify" and delta.get("clarified"):
                        qp = delta.get("query_plan") or {}
                        if delta.get("query"):
                            resolved_query = delta["query"]
                        if qp.get("repo_scope"):
                            resolved_scope = list(qp["repo_scope"])
                        trace_lines.append(
                            f"▸ clarified → query={resolved_query!r} "
                            f"scope={resolved_scope or '—'}"
                        )
                    elif node == "deep_research":
                        sq = len(delta.get("sub_questions") or [])
                        fd = len(delta.get("findings") or [])
                        trace_lines.append(f"▸ deep_research: {sq} sub-q → {fd} parallel branches")
                    elif node == "summarize":
                        trace_lines.append("▸ history summarized")
                    for m in delta.get("messages", []):
                        if isinstance(m, AIMessage) and m.tool_calls:
                            for tc in m.tool_calls:
                                tcid = tc.get("id") or tc["name"]
                                args = ", ".join(f"{k}={v!r}" for k, v in tc["args"].items())
                                if tcid in seen_tool_calls:
                                    for i, ln in enumerate(trace_lines):
                                        if ln == f"▸ {tc['name']}(…)":
                                            trace_lines[i] = f"▸ {tc['name']}({args})"
                                            break
                                else:
                                    trace_lines.append(f"▸ {tc['name']}({args})")
                                    seen_tool_calls.add(tcid)
                        elif isinstance(m, ToolMessage):
                            preview = str(m.content).replace("\n", " ")[:120]
                            trace_lines.append(f"  ↳ {preview}…")
                    if delta.get("answer"):
                        answer = delta["answer"]
                        streaming_text = ""
                    if "citations" in delta:
                        citations = delta["citations"] or []
                    if "iteration" in delta:
                        iteration = delta["iteration"]
                        trace_lines.append(f"▸ iterations: {iteration}")
                    push(); yield history, _BLANK_INPUT, None

    except Exception as e:
        history[-1]["content"] = f"**Error:** `{type(e).__name__}: {e}`"
        yield history, _BLANK_INPUT, None
        return

    try:
        final = _graph.get_state(config).values
    except Exception:
        final = {}

    final_query, store_ok, why = _store_eligible(
        user_msg, final, verbosity, was_resume=bool(pending_interrupt))
    if store_ok and answer:
        try:
            pid = cache_store(final_query, answer, citations)
            print(f"  [cache] STORED {pid[:8]} key={final_query!r} "
                  f"({len(answer)} chars)")
        except Exception as e:
            print(f"  [cache] store FAILED: {type(e).__name__}: {e}")
    elif answer:
        print(f"  [cache] not stored ({why})")

    try:
        push_scores(tracer, final)
    except Exception:
        pass
    trace_url = tracer.url()
    tracer.__exit__(None, None, None)

    push(done=True)
    if trace_url:
        history[-1]["content"] += f"\n\n[🔍 trace]({trace_url})"
    yield history, _BLANK_INPUT, None


# ── UI assembly ─────────────────────────────────────────────────────

def _new_session() -> tuple:
    sid = f"ui-{uuid.uuid4().hex[:8]}"
    return sid, gr.update(choices=[sid] + _list_sessions(), value=sid), []


def _select_session(sid: str) -> tuple:
    return sid, _load_history(sid)


def _gr_kwargs(cls, **candidate):
    """Pass only kwargs the installed Gradio version supports.
    If __init__ accepts **kwargs, pass everything (it forwards)."""
    params = inspect.signature(cls.__init__).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return candidate
    return {k: v for k, v in candidate.items() if k in params}


APP_NAME = "Code / Trace / Origin"
APP_SHORT = "CTO"
APP_TAGLINE = "From signal to source — cited to the line."


def build_ui() -> gr.Blocks:
    blocks_kwargs = _gr_kwargs(
        gr.Blocks, title=APP_SHORT, fill_height=True,
    )
    with gr.Blocks(**blocks_kwargs) as ui:
        # Gradio 6 moved Blocks(css=) to launch(); we use mount_gradio_app
        # which doesn't call launch(). Inject styles directly instead.
        gr.HTML(f"{_FONT_LINK}<style>{_CSS}</style>", visible=True)
        session_state = gr.State(value=f"ui-{uuid.uuid4().hex[:8]}")
        interrupt_state = gr.State(value=None)

        # gr.Sidebar is a TOP-LEVEL layout primitive — it must be a direct
        # child of Blocks, not nested in a Row. The main content goes at
        # the same level; Sidebar handles the split + collapse toggle.
        with gr.Sidebar(**_gr_kwargs(gr.Sidebar, open=False, width=300)):
            gr.HTML(
                '<h2 style="font-family: \'Space Grotesk\', sans-serif; '
                'font-weight: 500; letter-spacing: 0.02em; margin: 0.25rem 0;">'
                f'{APP_SHORT}</h2>'
                f'<p style="font-size: 0.78rem; '
                'color: var(--body-text-color-subdued); margin: 0 0 0.75rem;">'
                f'{APP_NAME}</p>'
            )
            with gr.Row():
                new_btn = gr.Button("➕ New", size="sm", scale=1)
                dark_btn = gr.Button("🌙", size="sm", scale=0, min_width=44)
            session_dd = gr.Dropdown(
                choices=_list_sessions(), label="Resume session",
                interactive=True, allow_custom_value=True,
            )
            sources_btn_side = gr.Button("📚 Sources", size="sm")
            jobs_btn_side = gr.Button("⏰ Jobs", size="sm")

        # Main chat area — sibling of Sidebar at Blocks top level so
        # fill_height applies directly.
        with gr.Row(elem_id="app-header-row"):
            with gr.Column(scale=1, min_width=0):
                gr.HTML(
                    '<div id="app-header">'
                    '  <h1>Code<span class="sep">/</span>'
                    'Trace<span class="sep">/</span>Origin</h1>'
                    f'  <p>{APP_TAGLINE}</p>'
                    '</div>'
                )
            sources_btn = gr.Button("📚 Sources", size="sm",
                                    scale=0, min_width=110,
                                    elem_id="sources-btn")
            jobs_btn = gr.Button("⏰ Jobs", size="sm",
                                 scale=0, min_width=90)

        chatbot = gr.Chatbot(**_gr_kwargs(
            gr.Chatbot,
            elem_id="chatbot",
            type="messages",
            show_copy_button=True,
            render_markdown=True,
            scale=1,
            show_label=False,
        ))
        with gr.Row(elem_id="input-row"):
            # MultimodalTextbox: text + drag-drop / paste image and text
            # files. Value shape is {"text": str, "files": [paths]}.
            msg = gr.MultimodalTextbox(**_gr_kwargs(
                gr.MultimodalTextbox,
                placeholder=("Ask about code, docs, logs, or runtime…  "
                             "Drop a screenshot or log file to attach."),
                file_types=["image", ".txt", ".md", ".log", ".json",
                            ".yaml", ".yml", ".py", ".java", ".go",
                            ".js", ".ts", ".sh", ".diff", ".patch"],
                file_count="multiple",
                show_label=False, autofocus=True, scale=1,
                container=False, submit_btn=False, stop_btn=False,
                elem_id="msg-input",
            ))
            # ⚙ settings: popover anchored to this column so
            # `position: absolute` places it above the button.
            with gr.Column(scale=0, min_width=44,
                           elem_id="settings-anchor"):
                settings_btn = gr.Button("⚙", size="sm", min_width=44)
                with gr.Column(visible=False,
                               elem_classes=["settings-pop"]) as settings_pop:
                    verbosity = gr.Radio(
                        choices=[("⚡ terse", "terse"),
                                 ("≡ normal", "normal"),
                                 ("📖 detail", "detailed")],
                        value="normal", label="Verbosity",
                        container=False, elem_id="verbosity-toggle",
                    )
                    trace_on = gr.Checkbox(
                        value=True, label="Phoenix tracing",
                        container=False,
                    )
                    gr.Markdown(
                        "Per-request — off skips span capture "
                        "(~6% faster, no 🔍 trace link).",
                        elem_classes=["settings-hint"],
                    )
            send = gr.Button("⏎", variant="primary", size="sm",
                             scale=0, min_width=44)
            stop = gr.Button("⏹", variant="stop", size="sm",
                             scale=0, min_width=44, interactive=False)
            clear = gr.Button("🗑", size="sm", scale=0, min_width=44)

        # Sources modal: rendered LAST so it paints above the chatbot.
        # Outer Column = full-screen overlay; inner Group = the card.
        with gr.Column(visible=False, elem_id="sources-overlay") as sources_modal:
            with gr.Group(elem_classes=["sources-card"]):
                with gr.Row():
                    with gr.Column(scale=1, min_width=0):
                        gr.Markdown("### 📚 Indexed Sources")
                    refresh_btn = gr.Button("↻", size="sm", scale=0,
                                            min_width=40)
                    close_btn = gr.Button("✕", size="sm", scale=0,
                                          min_width=40,
                                          elem_id="sources-close")
                sources = gr.Markdown(_sources_md(), elem_id="sources-body")

        # Jobs modal — same overlay pattern, wider card.
        with gr.Column(visible=False,
                       elem_id="sources-overlay") as jobs_modal:
            with gr.Group(elem_classes=["sources-card", "jobs-card"]):
                with gr.Row():
                    with gr.Column(scale=1, min_width=0):
                        gr.Markdown("### ⏰ Scheduled Jobs")
                    jobs_refresh = gr.Button("↻", size="sm",
                                             scale=0, min_width=40)
                    jobs_close = gr.Button("✕", size="sm", scale=0,
                                           min_width=40)
                jobs_hint = gr.Markdown("")
                jobs_table = gr.Dataframe(
                    headers=_JOBS_HEADERS, interactive=False,
                    wrap=False, elem_id="jobs-table",
                    **_gr_kwargs(gr.Dataframe,
                                 column_widths=["12%", "7%", "11%",
                                                "14%", "13%", "9%",
                                                "6%", "18%", "10%"]))
                with gr.Row():
                    jobs_pick = gr.Dropdown(
                        choices=[], label="Fire now",
                        interactive=True, scale=1)
                    jobs_fire = gr.Button("▶ Run",
                                          variant="primary",
                                          size="sm", scale=0,
                                          min_width=70)
                    gr.HTML('<a href="/jobs" target="_blank" '
                            'style="font-size:0.85em;'
                            'align-self:center">↗ full page</a>')
                jobs_status = gr.Markdown("")
                with gr.Accordion("Log tail (data/logs/scheduler.log)",
                                  open=False):
                    jobs_log = gr.HTML(elem_id="jobs-log")

        # ── Wiring ──────────────────────────────────────────────────

        def _start_buttons():
            return gr.update(interactive=False), gr.update(interactive=True)

        def _end_buttons():
            return gr.update(interactive=True), gr.update(interactive=False)

        # Submit via Enter or ⏎ button → run the streaming generator.
        # Capture the event handles so Stop can cancel them mid-stream.
        # concurrency_id groups both entry points into one pool;
        # limit=8 lets multiple tabs/users run in parallel (deep_research
        # can take 30-90s — without this, every other request blocks).
        chat_in = [msg, chatbot, session_state, interrupt_state,
                   verbosity, trace_on]
        chat_out = [chatbot, msg, interrupt_state]
        ev_submit = msg.submit(_start_buttons, outputs=[send, stop]) \
            .then(_chat, chat_in, chat_out,
                  concurrency_limit=8, concurrency_id="chat") \
            .then(_end_buttons, outputs=[send, stop])
        ev_send = send.click(_start_buttons, outputs=[send, stop]) \
            .then(_chat, chat_in, chat_out,
                  concurrency_limit=8, concurrency_id="chat") \
            .then(_end_buttons, outputs=[send, stop])

        stop.click(None, cancels=[ev_submit, ev_send]) \
            .then(_end_buttons, outputs=[send, stop])

        clear.click(lambda: ([], None, _BLANK_INPUT),
                    outputs=[chatbot, interrupt_state, msg])
        new_btn.click(_new_session, outputs=[session_state, session_dd, chatbot]) \
            .then(lambda: None, outputs=interrupt_state)
        session_dd.change(_select_session, inputs=session_dd,
                          outputs=[session_state, chatbot]) \
            .then(lambda: None, outputs=interrupt_state)
        dark_btn.click(None, js=_DARK_TOGGLE_JS)

        # Settings popover: toggle on ⚙, auto-close on send/submit,
        # and reflect current verbosity glyph on the button so the
        # active mode is visible without opening it.
        settings_open = gr.State(False)

        def _settings_toggle(is_open):
            return (not is_open), gr.update(visible=not is_open)

        _GLYPH = {"terse": "⚡", "normal": "≡", "detailed": "📖"}

        def _settings_badge(v, t):
            dot = "" if t else " •"  # • marks tracing OFF
            return gr.update(value=f"⚙ {_GLYPH[v]}{dot}")

        settings_btn.click(_settings_toggle, settings_open,
                           [settings_open, settings_pop])
        verbosity.change(_settings_badge, [verbosity, trace_on],
                         settings_btn)
        trace_on.change(_settings_badge, [verbosity, trace_on],
                        settings_btn)
        for ev in (msg.submit, send.click):
            ev(lambda: (False, gr.update(visible=False)),
               outputs=[settings_open, settings_pop])

        # Sources modal: open from header or sidebar button; ✕ closes;
        # ↻ refreshes. Content is fetched fresh each time it opens.
        def _open_sources():
            return gr.update(visible=True), _sources_md()
        sources_btn.click(_open_sources, outputs=[sources_modal, sources])
        sources_btn_side.click(_open_sources, outputs=[sources_modal, sources])
        close_btn.click(lambda: gr.update(visible=False),
                        outputs=sources_modal)
        refresh_btn.click(lambda: _sources_md(), outputs=sources)

        # Jobs modal: open/close/refresh + fire-now.
        import html as _html

        def _open_jobs():
            rows, names, tail, hint = _jobs_data()
            return (gr.update(visible=True), rows,
                    gr.update(choices=names,
                              value=names[0] if names else None),
                    f"<pre>{_html.escape(tail or '(empty)')}</pre>",
                    hint, "")

        def _refresh_jobs():
            rows, names, tail, hint = _jobs_data()
            return (rows, gr.update(choices=names),
                    f"<pre>{_html.escape(tail or '(empty)')}</pre>",
                    hint)

        def _fire_job(name):
            if not name:
                return "_(pick a job)_"
            try:
                import threading
                from ..scheduler import (load_jobs, run_job_now,
                                          ensure_schema)
                ensure_schema()
                j = next((x for x in load_jobs()
                          if x.name == name), None)
                if not j:
                    return f"_unknown job `{name}`_"
                threading.Thread(
                    target=run_job_now, args=(j,),
                    daemon=True, name=f"ui-fire-{name}").start()
                return (f"▶ fired `{name}` ({j.kind}) — "
                        f"refresh in a few seconds.")
            except Exception as e:
                return f"_failed: {type(e).__name__}: {e}_"

        for b in (jobs_btn, jobs_btn_side):
            b.click(_open_jobs,
                    outputs=[jobs_modal, jobs_table, jobs_pick,
                             jobs_log, jobs_hint, jobs_status])
        jobs_close.click(lambda: gr.update(visible=False),
                         outputs=jobs_modal)
        jobs_refresh.click(
            _refresh_jobs,
            outputs=[jobs_table, jobs_pick, jobs_log, jobs_hint])
        jobs_fire.click(_fire_job, inputs=jobs_pick,
                        outputs=jobs_status) \
            .then(_refresh_jobs,
                  outputs=[jobs_table, jobs_pick, jobs_log,
                           jobs_hint])

        # Page-load: apply saved theme + refresh session list.
        ui.load(None, js=_LOAD_THEME_JS)
        ui.load(lambda: gr.update(choices=_list_sessions()),
                outputs=session_dd)

    # Default concurrency for any handler that doesn't set its own
    # (button clicks, modal toggles, etc.) — these are all cheap.
    ui.queue(default_concurrency_limit=16)
    return ui
