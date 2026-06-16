"""
Subagent tools for /superdev (Phase 8.5-D).

  spawn(task, context)
      One child agent on the PARENT worktree, fresh context.
      Sequential — blocks until done. Child has host write/
      edit/glob/patch + RAG, but NO ask_user/todo/create_pr/
      commit/spawn. Destructive shell auto-denies. Returns
      the child's final answer.

  spawn_parallel(tasks, context)
      N children concurrently, each on its OWN sub-worktree
      forked from the parent branch tip. After all finish,
      each child's commits are cherry-picked into the parent
      (serially, in task order). Conflicts are NOT auto-
      resolved: that child's sub-worktree is kept and its
      path returned for manual resolution. Returns
      [{task, status: ok|conflict|error|empty, result,
        commits, path?}].

Depth limit = 1: children are built directly here via
`create_react_agent` (not via `build_superdev_graph`), so
they simply don't have spawn in their toolset.
"""

from __future__ import annotations

import json
import logging
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool, BaseTool
from langgraph.config import get_stream_writer

from ...vcs.worktree import Worktree, fork, merge_into, discard
from ..llm import llm
from .host import build_host_tools

log = logging.getLogger(__name__)

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from langgraph.prebuilt import create_react_agent

_MAX_PARALLEL = 4
_CHILD_RECURSION = 40


def _child_prompt(task: str, context: str, cwd: str,
                  parallel: bool) -> str:
    commit_note = (
        "When done, `commit(kind, area, desc)` your changes — "
        "the parent will cherry-pick them. "
        if parallel else
        "Do NOT commit — your edits land in the parent's "
        "worktree and the parent commits. ")
    return f"""You are a focused subagent inside /superdev.

Your ONLY task:
> {task}

Context from the parent agent:
{context or "(none)"}

cwd: {cwd}

You have host_shell/read/write/edit/glob/apply_patch,
docker_run, and the RAG tools (search_code, find_callers,
git_log, …). You CANNOT ask the user, spawn further agents,
track todos, or open a PR. Destructive shell commands are
auto-blocked (exit 126) — work around them.

{commit_note}Then return a 1–3 line summary of exactly what
you changed (file:line) and why. If you cannot complete the
task, return a line starting with `BLOCKED:` and the reason.
Be terse — the parent reads many of these."""


def _emit(write, child: int, kind: str, **kw):
    try:
        write({"skill_event": {"kind": kind, "child": child, **kw}})
    except Exception:
        pass


def _run_child(task: str, context: str, child_wt: Worktree,
               idx: int, *, parallel: bool, parent_tid: str,
               write) -> dict:
    tools = build_host_tools(
        child_wt, [child_wt.path],
        child_mode=True,
        with_commit=parallel,
        confirm=lambda cmd, why: False,
    )
    agent = create_react_agent(
        model=llm("agent_heavy", temperature=0,
                  streaming=False, max_tokens=4096),
        tools=tools,
        prompt=_child_prompt(task, context,
                             str(child_wt.path), parallel),
        name=f"spawn-{idx}",
    )
    cfg = {"configurable":
           {"thread_id": f"{parent_tid}/spawn/{idx}"},
           "recursion_limit": _CHILD_RECURSION}

    _emit(write, idx, "spawn", task=task[:120],
          mode="par" if parallel else "seq",
          cwd=str(child_wt.path))
    final = ""
    n_tools = 0
    try:
        for ev in agent.stream({"messages": []}, config=cfg,
                                stream_mode="updates"):
            for _, d in ev.items():
                if not isinstance(d, dict):
                    continue
                for m in d.get("messages") or []:
                    if isinstance(m, AIMessage) and getattr(
                            m, "tool_calls", None):
                        for tc in m.tool_calls:
                            n_tools += 1
                            args = {
                                k: (f"<{len(v)} chars>"
                                    if isinstance(v, str)
                                    and len(v) > 80 else v)
                                for k, v in
                                (tc.get("args") or {}).items()}
                            _emit(write, idx, "tool_call",
                                  name=tc.get("name"),
                                  args=args)
                    elif isinstance(m, ToolMessage):
                        _emit(write, idx, "tool_result",
                              name=getattr(m, "name", None),
                              preview=str(m.content)
                                  .replace("\n", " ")[:120])
                    elif isinstance(m, AIMessage) and m.content:
                        final = m.content
        status = "ok"
        err = None
    except Exception as e:
        status = "error"
        err = f"{type(e).__name__}: {e}"
        log.exception("[spawn %d] failed", idx)
        final = final or err

    _emit(write, idx, "spawn_done", status=status,
          summary=(final or "")[:160], n_tools=n_tools)
    return {"task": task, "status": status,
            "result": final or "(no output)", "error": err,
            "n_tools": n_tools}


def make_spawn_tools(parent_wt: Worktree | None,
                     parent_tid: str) -> list[BaseTool]:
    """Returns [spawn, spawn_parallel] bound to this session's
    worktree. Empty list if no worktree (shell-only mode)."""
    if parent_wt is None:
        return []

    write = get_stream_writer
    merge_lock = threading.Lock()

    @tool
    def spawn(task: str, context: str = "") -> str:
        """Run ONE focused subagent on this worktree with a
        fresh context. Blocks until it finishes. The child
        has host write/edit/glob/patch + RAG tools but cannot
        ask you, spawn further, commit, or open a PR — its
        edits land directly in your worktree; YOU commit. Use
        for "do step N of the plan with a clean head" or
        "investigate X and report back". `context` is what
        the child needs to know (findings, file paths) — it
        starts with NO conversation history. Returns the
        child's 1–3 line summary."""
        out = _run_child(task, context, parent_wt, 0,
                         parallel=False, parent_tid=parent_tid,
                         write=write())
        return out["result"]

    @tool
    def spawn_parallel(tasks: list[str],
                       context: str = "") -> str:
        """Run N subagents CONCURRENTLY (max 4), each on its
        own isolated fork of this worktree. After all finish,
        each child's commits are cherry-picked back into your
        worktree in order. Conflicts are NOT auto-resolved —
        that child's fork is kept and its path returned. Use
        for "apply the same fix to N independent files" or
        "implement these N unrelated items". REQUIRES your
        worktree to be clean — `commit` any WIP first.
        Returns JSON: [{task, status: ok|conflict|error|
        empty, result, commits, path?}]."""
        if not tasks:
            return "ERROR: tasks list is empty"
        if len(tasks) > 12:
            return (f"ERROR: {len(tasks)} tasks > 12 — split "
                    f"into batches")
        if parent_wt.has_changes():
            return ("ERROR: parent worktree has uncommitted "
                    "changes — `commit` first so forks branch "
                    "from a clean tip")

        w = write()
        subs: list[Worktree] = []
        try:
            for i in range(len(tasks)):
                subs.append(fork(parent_wt, i))
        except Exception as e:
            for s in subs:
                discard(s)
            return f"ERROR: fork failed: {e}"

        _emit(w, -1, "spawn_parallel", n=len(tasks))

        def _one(i: int) -> dict:
            return _run_child(tasks[i], context, subs[i], i,
                              parallel=True,
                              parent_tid=parent_tid, write=w)

        with ThreadPoolExecutor(
                max_workers=min(len(tasks),
                                _MAX_PARALLEL)) as pool:
            results = list(pool.map(_one, range(len(tasks))))

        # Merge serially in task order so cherry-picks don't
        # race the parent worktree.
        for i, (sub, res) in enumerate(zip(subs, results)):
            with merge_lock:
                from ...vcs.worktree import _git
                shas = _git(sub.path,
                            ["rev-list", "--reverse",
                             f"{parent_wt.branch}..HEAD"],
                            check=False).split()
                res["commits"] = [s[:8] for s in shas]
                if res["status"] != "ok":
                    discard(sub)
                    continue
                if not shas:
                    res["status"] = "empty"
                    discard(sub)
                    continue
                clean, picked = merge_into(sub, parent_wt)
                if clean:
                    discard(sub)
                    _emit(w, i, "spawn_merge", status="ok",
                          commits=len(picked))
                else:
                    res["status"] = "conflict"
                    res["path"] = str(sub.path)
                    res["result"] += (
                        f"\n[CONFLICT — kept at {sub.path}; "
                        f"resolve manually or discard]")
                    _emit(w, i, "spawn_merge",
                          status="conflict", path=str(sub.path))

        return json.dumps(results, indent=2)

    return [spawn, spawn_parallel]
