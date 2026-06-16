"""
/superdev graph (Phase 8-C + 8.5-G wrapper).

A SECOND compiled graph with a disjoint capability set: host
shell, host write/edit, docker_run(network=True), create_pr. The
read-only graph (graph.py) never imports tools/host.py, so host
tools aren't even *available* there — compile-time isolation, not
prompt-time.

  START → reset → input_guard
                     ├ blocked  → respond → END
                     └ continue → load_memories → superdev_loop → eval_route
                                                                    ├ skip  → output_guard
                                                                    └ grade → evaluator → output_guard
                                                                                              │
                                                                                              ▼
                                                                                          respond
                                                                                              │
                                                                            [summarize?]      │
                                                                                              ▼
                                                                                       save_memories → END

8.5-G wrapper: load_memories / evaluator / output_guard /
summarize_history / save_memories are spliced around superdev_loop —
SAME node implementations as graph.py, no duplication. Routing is
turn-aware: action turns (anything that wasn't pure read-only tools)
skip evaluator (no answer to grade) but still pass through
output_guard (no-op when retrieved_chunks is empty).

Built per-session via `build_superdev_graph(wt, allowed_dirs)` so
the worktree is closed over and the LLM cannot pick it.
"""

from __future__ import annotations

import json
import logging
import warnings
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, interrupt

from ..config import CTO_SUPERDEV_ENABLED
from ..memory.checkpointer import get_checkpointer
from ..skills import generic as generic_skills
from ..vcs.worktree import Worktree
from ._turn_kind import classify_turn
from .llm import llm
from .state import AgentState
from .nodes.guards import input_guard, input_guard_route, output_guard
from .nodes.evaluator import evaluator
from .nodes.memories import load_memories, save_memories
from .nodes.reset import reset_turn
from .nodes.respond import respond
from .nodes.summarize import summarize_history, should_summarize
from .playbooks import render as render_playbooks
from .tools.host import build_host_tools

log = logging.getLogger(__name__)

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from langgraph.prebuilt import create_react_agent


# Same RAG tools the inner agent uses to populate retrieved_chunks.
_CHUNK_TOOLS = ("search_code", "search", "search_docs")


def _system_prompt(wt: Worktree | None, cwd: Path) -> str:
    if wt is not None:
        ctx = f"""You are operating inside a git worktree:

  cwd:    {wt.path}
  repo:   {wt.repo}
  branch: {wt.branch}

The worktree is your sandbox — every write should land there.
The user reviews your changes via `create_pr`; you NEVER push to
or merge into main/master."""
    else:
        ctx = f"""You are in **shell-only** mode (no repo):

  cwd: {cwd}

You have host_shell / host_read / docker_run / ask_user. You do
NOT have write, edit, commit, or create_pr — there is no
throwaway branch here, so file mutation is the user's job. If a
task needs code edits, tell the user to re-enter with
`/superdev <repo>`. Use host_shell for everything else (cloud
CLI, builds, scripts, diagnostics)."""
    return f"""You are CTO in **superdev mode** — a coding agent
running directly on the user's machine. {ctx}

You are NOT an advisor — you are an operator. Never tell the
user to "run this in your terminal"; YOU run it via host_shell
and report the actual output. Your env inherits the user's
shell (PATH, AWS_*/KUBE*/GCLOUD_*, ~/.aws, ~/.kube, …), so
`aws`, `kubectl`, `gh`, `gcloud`, `terraform` all work directly.

Tools:
- host_shell(cmd)         — runs on the host. Destructive patterns
                            pause for confirmation. cwd defaults to
                            the worktree. Use this for ANY shell
                            task — cloud CLI, build, test, grep.
- host_read / host_write / host_edit — write/edit are jailed to
                            the worktree (+ user-allowed dirs).
- docker_run(cmd, image)  — when you'd rather isolate. Worktree
                            mounted at /work:rw; network ON.
- commit(kind, area, desc)→ stage + commit with the enforced
                            convention.
- create_pr(title, body)  — push the branch and open a PR/MR.
- host_glob(pattern)      — list files under the worktree.
- host_apply_patch(diff)  — apply a unified diff (multi-file).
- todo(action, …)         — set/add/done a working checklist;
                            shown live in the toolbar.
- spawn(task, context)    — run ONE focused subagent on this
                            worktree with a fresh context. It
                            edits; YOU commit. Use for "do
                            step N with a clean head."
- spawn_parallel(tasks)   — run N subagents concurrently,
                            each on an isolated fork; their
                            commits cherry-pick back
                            (conflicts kept, not auto-fixed).
                            **`commit` your WIP first.** Use
                            for "apply the same fix to N
                            independent files."
                            Children cannot ask_user, spawn
                            further, or run destructive cmds.
- ask_user(question)      — ALWAYS confirm before any action that
                            could be destructive, irreversible, or
                            outside the worktree. When in doubt, ask.

Code intelligence (read-only, over the WHOLE indexed corpus —
not just this worktree, with embeddings + AST + call graph):
- **search** — cascade over code → docs → Confluence; semantic
  + reranked. **USE FIRST for natural-language questions**:
  "how is X handled?", "where is X used?", "what does Y do?".
  Casing doesn't matter; one call usually suffices.
- search_code / search_docs — restrict to one tier.
- find_symbol / find_callers / find_callees — exact-name graph
  lookups (case-insensitive by default). Use after `search`
  when you have a specific symbol to chase.
- grep — case-insensitive regex over all repos (rg under the
  hood). Use for error strings, config keys, literal patterns.
- read_file — fetch a specific file at a line range.
- repo_info / git_log / git_show / git_blame — repo metadata
  and history.

**PREFER these over `host_shell rg` / `cat` / `find` for
discovery — they are NOT optional.** They search the FULL
indexed corpus across every repo with semantic embeddings + AST
awareness; `host_shell rg` only sees raw bytes in the worktree.
Fall back to `host_shell` ONLY when the indexed tools return
nothing useful (rare) or when the data genuinely isn't in the
corpus (env vars, running processes, build output, shell tool
versions).

**Repo scope rule — read carefully.** find_symbol /
find_callers / find_callees / grep / search_code all accept an
optional `repo=` argument. **Default to OMITTING it** (no
repo filter → all indexed repos). DO NOT pass `repo=<cwd's
repo>` just because your cwd happens to be that worktree —
the user is asking about THEIR code corpus, not just your
sandbox. Pass `repo=X` ONLY when (a) the user explicitly named
repo X in the current turn, or (b) you already searched all
repos, found multiple candidate matches, and need to narrow.
Passing `repo='<your-cwd-repo>'` by default to a question
about a symbol that lives in another repo returns zero hits
and misleads the user.

Workflow per task: todo(set, items=[…]) → understand
(find_callers, search_code, git_log) → edit (host_edit /
host_apply_patch) → build/test (host_shell or docker_run) →
todo(done, …) per step → commit → ask_user("Open a PR?") →
create_pr.

**Hard rule — never end a turn with uncommitted changes.**
If you wrote or edited anything in the worktree, your LAST
tool calls before any final summary MUST be `commit(...)`
then `ask_user("Open a PR?")` (and `create_pr` if they say
yes). A turn that says "done" while `git status` is dirty is
a failure. If the build/tests couldn't run (tooling missing,
exit 127), say so explicitly and STILL commit + ask — the
reviewer decides, not you.

Discovery: prefer `host_glob("**/*.java")` or
`find_symbol`/`search_code` over chains of `ls` calls — one
glob beats seven `ls`.

Output: be concise. When a host_shell result is already
tabular/JSON, wrap it in a ``` fence verbatim — do NOT re-render
it row-by-row as markdown. Summarize in one line above the fence
(e.g. "12 instances, 3 running"). One tool call per step.

**Citations (read/lookup turns only)** — when you ANSWER a
question by reading the indexed corpus (search / search_code /
search_docs / find_symbol / find_callers / find_callees / grep
/ read_file / git_log / git_show / git_blame), cite every
claim inline as [SOURCE_N] and append a footnote block at the
end of your answer in the form:

[SOURCE_N]: repo/path/to/file.ext

For web/Confluence results that carry a `url`, use the URL as
the footnote location instead of the filepath:

[SOURCE_N]: https://confluence.../pages/12345

N starts at 1 and increments per distinct source. Reuse the
same N for the same source. Skip citations entirely on ACTION
turns (host_write/edit/commit/create_pr/docker_run) — the
deliverable IS the change; the user sees the tool stream."""


def _extract_chunks(new_msgs: list) -> list[dict]:
    """Pull search/search_code/search_docs results out of ToolMessages
    in this turn's delta so the evaluator + output_guard see real
    retrieved chunks (same logic as nodes/agent_loop.py)."""
    out: list[dict] = []
    for m in new_msgs:
        if (not isinstance(m, ToolMessage)
                or getattr(m, "name", "") not in _CHUNK_TOOLS):
            continue
        body = m.content
        # Some adapters wrap content as list-of-blocks.
        if isinstance(body, list):
            body = "".join(
                b.get("text", "") for b in body
                if isinstance(b, dict) and b.get("type") == "text")
        if not isinstance(body, str):
            continue
        # Strip the <retrieved>…</retrieved> sentinel agent_loop
        # adds — superdev's tools return raw JSON, but be defensive.
        body = body.strip()
        if body.startswith("<retrieved>") and body.endswith("</retrieved>"):
            body = body[len("<retrieved>"):-len("</retrieved>")].strip()
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, list):
            out.extend(parsed)
        elif (isinstance(parsed, dict)
              and isinstance(parsed.get("results"), list)):
            out.extend(parsed["results"])
    return out


def build_superdev_graph(wt: Worktree | None,
                         allowed_dirs: list[Path] | None = None,
                         cwd: Path | None = None,
                         playbooks: list[str] | None = None,
                         todo_state: list | None = None,
                         session_tid: str = "superdev"):
    if not CTO_SUPERDEV_ENABLED:
        raise RuntimeError(
            "CTO_SUPERDEV_ENABLED is false — refusing to compile "
            "the superdev graph.")

    base = wt.path if wt else (cwd or Path.cwd()).resolve()
    roots = allowed_dirs if allowed_dirs is not None else [base]
    if base not in roots:
        roots.insert(0, base)
    tools = build_host_tools(wt, roots, todo_state=todo_state)
    if wt is not None:
        from .tools.spawn import make_spawn_tools
        tools += make_spawn_tools(wt, f"{session_tid}/inner")

    prompt = (render_playbooks(playbooks or [])
              + _system_prompt(wt, base))
    inner = create_react_agent(
        model=llm("agent_heavy", temperature=0, streaming=True,
                  max_tokens=8192),
        tools=tools,
        prompt=prompt,
        checkpointer=get_checkpointer(),
        name="superdev",
    )

    def reset(state: AgentState) -> dict:
        # Reuse the canonical per-turn reset, then stamp the route so
        # downstream nodes (evaluator skip-checks, store_eligible)
        # know they're in superdev. turn_kind cleared so each turn
        # classifies fresh.
        delta = reset_turn(state)
        delta["route"] = "superdev"
        delta["turn_kind"] = ""
        delta["answer"] = ""
        delta["iteration"] = 0
        return delta

    def _inner_cfg(parent_cfg: dict | None) -> dict:
        tid = ((parent_cfg or {}).get("configurable") or {}) \
            .get("thread_id") or "superdev"
        return {"configurable": {"thread_id": f"{tid}/inner"},
                "recursion_limit": 80}

    def superdev_loop(state: AgentState,
                      config: RunnableConfig = None) -> dict:
        """Drive the inner host-tool agent. Streams each tool
        call/result as a `skill_event` so the CLI renders ⏺/⎿
        live, and bridges any inner interrupt() (host_shell
        destructive-confirm, ask_user) up to the parent graph so
        resume works on a separate inner checkpoint thread."""
        write = get_stream_writer()
        cfg = _inner_cfg(config)

        # Resume bridge: if the inner agent is paused on its own
        # checkpoint, surface its question via the PARENT
        # interrupt(). On parent-resume this same call returns the
        # answer; otherwise start fresh from the parent's history.
        inner_st = inner.get_state(cfg)
        pending = next(
            (t.interrupts[0].value
             for t in (getattr(inner_st, "tasks", ()) or ())
             if getattr(t, "interrupts", None)), None)
        if pending is not None:
            payload = Command(resume=interrupt(pending))
        else:
            msgs = list(state.get("messages") or [])
            # Generic-skill splice (Phase 8.5-E): if the latest user
            # message is `/<skill-name> [args]`, replace it with the
            # rendered skill body. Resolution is per-turn (not at
            # compile time) so users can invoke skills mid-session.
            msgs, spliced = generic_skills.maybe_splice(msgs)
            if spliced is not None:
                write({"skill_event": {
                    "kind": "skill_invoke",
                    "name": spliced.name,
                    "source": spliced.source}})
            payload = {"messages": msgs}

        write({"skill_event": {
            "kind": "start", "skill": "superdev",
            "repo": wt.repo if wt else None,
            "mode": "host" if wt else "shell",
            "cid": str(base)}})
        while True:
            interrupted = None
            for mode, ev in inner.stream(
                    payload, config=cfg,
                    stream_mode=["updates", "messages",
                                 "custom"]):
                if mode == "custom":
                    write(ev)
                    continue
                if mode == "messages":
                    chunk, meta = ev
                    content = getattr(chunk, "content", None)
                    if content:
                        # Anthropic models return `content` as a list
                        # of blocks when text + tool_use are
                        # interleaved. CLI's streaming concat expects
                        # str — extract just the text blocks here.
                        if isinstance(content, list):
                            text = "".join(
                                b.get("text", "") for b in content
                                if isinstance(b, dict)
                                and b.get("type") == "text")
                            if not text:
                                continue
                            content = text
                        write({"skill_event": {
                            "kind": "text",
                            "delta": content}})
                    continue
                if "__interrupt__" in ev:
                    interrupted = ev["__interrupt__"][0].value
                    break
                for _, d in ev.items():
                    if not isinstance(d, dict):
                        continue
                    for m in d.get("messages") or []:
                        if isinstance(m, AIMessage) and getattr(
                                m, "tool_calls", None):
                            for tc in m.tool_calls:
                                args = {
                                    k: (f"<{len(v)} chars>"
                                        if isinstance(v, str)
                                        and len(v) > 120 else v)
                                    for k, v in
                                    (tc.get("args") or {}).items()}
                                write({"skill_event": {
                                    "kind": "tool_call",
                                    "name": tc.get("name"),
                                    "args": args}})
                        elif isinstance(m, ToolMessage):
                            write({"skill_event": {
                                "kind": "tool_result",
                                "name": getattr(m, "name", None),
                                "preview": str(m.content)
                                    .replace("\n", " ")[:160]}})
            if interrupted is None:
                break
            payload = Command(resume=interrupt(interrupted))
        write({"skill_event": {"kind": "end", "skill": "superdev"}})

        msgs = inner.get_state(cfg).values.get("messages", [])
        new = msgs[len(state.get("messages") or []):]
        final = next(
            (m for m in reversed(msgs)
             if isinstance(m, AIMessage)
             and not getattr(m, "tool_calls", None)),
            None)

        turn_kind = classify_turn(new)
        chunks = _extract_chunks(new) if turn_kind == "read" else []

        return {
            "messages": new,
            "answer": (final.content if final else "") or "(no output)",
            "iteration": sum(
                1 for m in new if isinstance(m, AIMessage)),
            "turn_kind": turn_kind,
            "retrieved_chunks": chunks,
        }

    def blocked(state: AgentState) -> dict:
        return {"answer": state.get("answer")
                or "⛔ blocked by input guard."}

    def _eval_route(state: AgentState) -> str:
        """Skip evaluator on action turns — there's no answer to grade
        against retrieval, and the user already sees commit/edit
        confirmations from the tool stream."""
        return "skip" if state.get("turn_kind") != "read" else "grade"

    g = StateGraph(AgentState)
    g.add_node("reset", reset)
    g.add_node("input_guard", input_guard)
    g.add_node("load_memories", load_memories)
    g.add_node("blocked", blocked)
    g.add_node("superdev_loop", superdev_loop)
    g.add_node("evaluator", evaluator)
    g.add_node("output_guard", output_guard)
    g.add_node("respond", respond)
    g.add_node("summarize", summarize_history)
    g.add_node("save_memories", save_memories)

    g.add_edge(START, "reset")
    g.add_edge("reset", "input_guard")
    g.add_conditional_edges(
        "input_guard", input_guard_route,
        {"continue": "load_memories", "blocked": "blocked"})
    g.add_edge("blocked", "respond")
    g.add_edge("load_memories", "superdev_loop")
    g.add_conditional_edges(
        "superdev_loop", _eval_route,
        {"grade": "evaluator", "skip": "output_guard"})
    g.add_edge("evaluator", "output_guard")
    g.add_edge("output_guard", "respond")
    g.add_conditional_edges(
        "respond", should_summarize,
        {"summarize": "summarize", "skip": "save_memories"})
    g.add_edge("summarize", "save_memories")
    g.add_edge("save_memories", END)

    log.info("[superdev] compiled graph (mode=%s cwd=%s branch=%s "
             "roots=%d tools=%d playbooks=%s)",
             "host" if wt else "shell", base,
             wt.branch if wt else "-", len(roots), len(tools),
             playbooks or "—")

    # Store enables load_memories / save_memories. Fail-soft if
    # PostgresStore is unavailable (same pattern as graph.py).
    store = None
    try:
        from ..memory.store import get_store
        store = get_store()
    except Exception as e:
        log.warning("[superdev] PostgresStore unavailable (%s); "
                    "memories disabled", e)
    return g.compile(checkpointer=get_checkpointer(), store=store)
