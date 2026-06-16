"""
skill_runner node (Phase 7-F).

Wraps `create_react_agent` with: prompt = preamble + skill.body,
tools = narrowed `build_skill_tools()` set, recursion_limit =
skill.max_iter. For `sandbox.mode: persistent`, this node also owns
the container lifecycle (start → checkpoint cid in state → stop).

The inner agent runs with its OWN checkpointer thread
(`<parent>/skill/<name>`) so multi-turn `ask_user` interrupts can be
resumed without losing the skill's tool-call progress. The bridge
between inner-interrupt and parent-interrupt is the loop in
`skill_run`: when the inner agent pauses, we surface its request via
the PARENT graph's `interrupt()`; on resume, langgraph re-enters
this node, `interrupt()` returns the user's answer, and we
`Command(resume=…)` it into the still-checkpointed inner agent.

KNOWN LIMITATION (v1): if a single skill run fires `ask_user` more
than once, the second resume re-plays the first interrupt() call
(langgraph indexes resume values per-node-execution). The bridge
handles ONE interrupt cleanly; multi-interrupt skills should batch
their questions. Tracked for a follow-up using a proper subgraph-
as-node mount once the basic flow is proven.
"""

import logging
import warnings
from datetime import date

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.types import Command, interrupt

from pathlib import Path

from ..agents.llm import llm
from ..agents.state import AgentState
from ..memory.checkpointer import get_checkpointer
from ..sandbox.docker import DockerSandbox, SKILL_PREFIX
from ..vcs import worktree as _wt
from .registry import Skill, get as get_skill, list_skills
from .tools import build_skill_tools

log = logging.getLogger(__name__)

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from langgraph.prebuilt import create_react_agent


# ── preamble ──────────────────────────────────────────────────────

def _preamble(skill: Skill, repo: str | None) -> str:
    parts = [
        f"# Skill: {skill.name}",
        "",
        f"Target repository: `{repo or '(none specified)'}`.",
    ]
    if skill.needs_shell:
        if skill.sandbox.mode == "persistent":
            parts.append(
                "All `run_shell_command` calls share ONE container "
                "for this run. Installed packages, files under "
                "/work, env vars, and cwd persist across calls. "
                f"Image: `{skill.sandbox.image}`. /repo is mounted "
                "READ-ONLY — copy to /work before mutating. The "
                "container is destroyed when this skill completes."
            )
        else:
            parts.append(
                "Each `run_shell_command` starts a FRESH container "
                f"(`{skill.sandbox.image}`). NOTHING persists "
                "between calls — write self-contained one-shot "
                "commands. /work is per-call scratch; /repo is "
                "read-only."
            )
        if not skill.sandbox.network:
            parts.append(
                "The sandbox has NO network — do not attempt to "
                "download or install."
            )
    else:
        parts.append(
            "This skill has NO shell access. Use the read tools "
            "(read_file, search_code, grep_search, repo_info, "
            "find_*, git_log) to gather information."
        )
    if skill.sandbox.worktree:
        parts.append(
            "/work is a git WORKTREE of /repo on a throwaway "
            "branch. Edit files under /work (NOT /repo), "
            "`git -C /work add/commit` per fix, then call "
            "`create_pr(title, body)` to push and open a PR for "
            "human review. You CANNOT push to main/master."
        )
        mutation_note = (
            "Repo mutation: edit /work, commit there, then "
            "`create_pr`. Never write to /repo.")
    else:
        mutation_note = (
            "Repo mutation (git commit/push/checkout) is NOT "
            "available.")
    parts += [
        "",
        "Use `write_report(repo, filename, content)` for the final "
        "deliverable — never write into /repo. Use `ask_user` only "
        "when you genuinely cannot proceed without a human "
        f"decision. {mutation_note}",
        "",
        f"Today's date: {date.today().isoformat()}.",
        "",
        "---",
        "",
    ]
    return "\n".join(parts)


# ── inner agent build (cached per (skill, repo, cid)) ─────────────

_agent_cache: dict[tuple, object] = {}


def _agent_for(skill: Skill, repo: str | None, cid: str | None,
               wt: _wt.Worktree | None, *,
               headless: bool = False,
               headless_answers: dict | None = None):
    key = (skill.name, repo, cid, wt.branch if wt else None,
           headless)
    a = _agent_cache.get(key)
    if a is None:
        a = create_react_agent(
            # max_tokens raised: write_report carries the full
            # markdown report as a tool argument; the gateway's
            # default cap truncates it mid-emission and the call
            # arrives missing `content`. 16k covers ~12 KB reports.
            model=llm("agent", temperature=0, streaming=False,
                      max_tokens=16384),
            tools=build_skill_tools(
                skill, repo, cid, worktree=wt,
                headless=headless,
                headless_answers=headless_answers),
            prompt=_preamble(skill, repo) + skill.body,
            checkpointer=get_checkpointer(),
            name=f"skill:{skill.name}",
        )
        _agent_cache[key] = a
    return a


def _inner_cfg(parent_cfg: dict | None, skill: Skill,
               repo: str | None) -> dict:
    parent_tid = ((parent_cfg or {}).get("configurable") or {}) \
        .get("thread_id") or "skill"
    return {
        "configurable": {
            "thread_id": f"{parent_tid}/skill/{skill.name}",
        },
        "recursion_limit": max(4, skill.max_iter * 2),
    }


# ── the LangGraph node ────────────────────────────────────────────

def skill_runner(state: AgentState,
                 config: RunnableConfig = None) -> dict:
    name = state.get("skill_name")
    sk = get_skill(name or "")
    if sk is None:
        avail = ", ".join(s.name for s in list_skills()) or "(none)"
        msg = f"Unknown skill `{name}`. Available: {avail}."
        return {"answer": msg, "route": "skill",
                "messages": [AIMessage(content=msg)],
                "skill_cid": None}

    repo = ((state.get("query_plan") or {}).get("repo_scope")
            or [None])[0]
    cid = state.get("skill_cid")

    # ── worktree (Phase 8-D): rehydrate or create ────────────────
    wt: _wt.Worktree | None = None
    if sk.sandbox.worktree:
        if not repo:
            msg = (f"⛔ Skill `{sk.name}` has `sandbox.worktree: "
                   f"true` but no repo was specified.")
            return {"answer": msg, "route": "skill",
                    "messages": [AIMessage(content=msg)],
                    "skill_cid": None, "skill_wt": None}
        prev = state.get("skill_wt")
        if prev and Path(prev["path"]).exists():
            wt = _wt.Worktree(
                repo=prev["repo"], path=Path(prev["path"]),
                branch=prev["branch"], src=Path(prev["src"]))
        else:
            # Deterministic session dir = inner thread_id so a
            # parent-level interrupt() replay re-attaches to the
            # SAME worktree (create() → attach() when dir exists)
            # instead of orphaning the edits.
            sess = (_inner_cfg(config, sk, repo)["configurable"]
                    ["thread_id"].replace("/", "_"))
            wt = _wt.create(repo, prefix=sk.name, session=sess)

    # ── persistent-mode container start/re-attach ────────────────
    if sk.sandbox.mode == "persistent":
        if not DockerSandbox.available():
            msg = (f"⛔ Docker is not available — skill `{sk.name}` "
                   f"requires `sandbox.mode: persistent`.")
            if wt:
                _wt.discard(wt)
            return {"answer": msg, "route": "skill",
                    "messages": [AIMessage(content=msg)],
                    "skill_cid": None, "skill_wt": None}
        if not (cid and DockerSandbox.alive(cid)):
            cid = DockerSandbox().start(
                repo=repo, image=sk.sandbox.image,
                network=sk.sandbox.network, memory=sk.sandbox.memory,
                run_timeout=sk.sandbox.run_timeout,
                worktree=wt.path if wt else None,
                env_passthrough=sk.sandbox.env_passthrough,
                mounts=sk.sandbox.mounts,
            )

    headless = bool(state.get("headless"))
    agent = _agent_for(sk, repo, cid, wt,
                       headless=headless,
                       headless_answers=state.get(
                           "headless_answers"))
    inner_cfg = _inner_cfg(config, sk, repo)

    # ── interrupt bridge ──────────────────────────────────────────
    # Decide payload: if the inner agent is mid-run (paused on its
    # own checkpoint), we're in a resume cycle — surface its
    # pending question via the PARENT interrupt(). On a fresh
    # parent run, interrupt() raises and the parent checkpoints
    # here; on parent-resume, this same call RETURNS the user's
    # answer, which we forward into the inner agent.
    inner_st = agent.get_state(inner_cfg)
    pending = None
    for t in getattr(inner_st, "tasks", ()) or ():
        if getattr(t, "interrupts", None):
            pending = t.interrupts[0].value
            break

    if pending is not None:
        if headless:
            payload = Command(resume="skip")
        else:
            answer = interrupt(pending)        # ← parent-level
            payload = Command(resume=answer)
    elif inner_st.values.get("messages"):
        # Inner thread already ran to completion in a prior turn —
        # this shouldn't normally happen (each parent turn gets a
        # fresh derived tid via parent_tid). Treat as fresh.
        payload = {"messages": list(state.get("messages") or [])}
    else:
        payload = {"messages": list(state.get("messages") or [])}

    # ── drive the inner agent (one pass; loop if it interrupts) ───
    # Re-emit each inner tool-call/result as a CUSTOM stream event
    # so the parent graph's caller (CLI/UI/SSE) can render live
    # ⏺/⎿ progress instead of one opaque skill_runner delta at the
    # end. Outside a streaming parent, get_stream_writer() returns
    # a no-op.
    write = get_stream_writer()
    write({"skill_event": {"kind": "start", "skill": sk.name,
                            "repo": repo, "mode": sk.sandbox.mode,
                            "cid": cid,
                            "branch": wt.branch if wt else None}})
    interrupted = None
    while True:
        interrupted = None
        for ev in agent.stream(payload, config=inner_cfg,
                               stream_mode="updates"):
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
                            args = dict(tc.get("args") or {})
                            # Don't push large/secret-ish args
                            # through the event channel; the trace
                            # line just needs to be readable.
                            for k, v in list(args.items()):
                                if isinstance(v, str) and len(v) > 120:
                                    args[k] = f"<{len(v)} chars>"
                            write({"skill_event": {
                                "kind": "tool_call",
                                "name": tc.get("name"),
                                "args": args,
                            }})
                    elif isinstance(m, ToolMessage):
                        write({"skill_event": {
                            "kind": "tool_result",
                            "name": getattr(m, "name", None),
                            "preview": str(m.content)
                                .replace("\n", " ")[:160],
                        }})
        if interrupted is None:
            break
        # Inner paused again. Surface to parent (raises on first
        # encounter; returns the answer on the resume re-entry).
        if headless:
            log.info("[skill_runner] headless: auto-resume "
                     "interrupt with 'skip'")
            payload = Command(resume="skip")
        else:
            answer = interrupt(interrupted)
            payload = Command(resume=answer)
    write({"skill_event": {"kind": "end", "skill": sk.name}})

    # ── collect result ────────────────────────────────────────────
    final_st = agent.get_state(inner_cfg).values
    msgs = final_st.get("messages", [])
    new = msgs[len(state.get("messages") or []):]
    final = next(
        (m for m in reversed(msgs)
         if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None)),
        None,
    )
    n_tools = sum(1 for m in new if isinstance(m, ToolMessage))
    log.info("[skill_runner] %s repo=%s msgs=%d tools=%d cid=%s",
             sk.name, repo, len(new), n_tools, cid)

    delta: dict = {
        "messages": new,
        "answer": (final.content if final else "") or "(skill produced no answer)",
        "route": "skill",
        "iteration": sum(1 for m in new if isinstance(m, AIMessage)),
        "skill_cid": None,
        "skill_wt": None,
    }

    # ── persistent-mode teardown ─────────────────────────────────
    if cid:
        DockerSandbox.stop(cid)
    # Worktree: keep if commits exist (it IS the PR branch);
    # otherwise discard so the indexer never sees WIP and orphans
    # don't accumulate.
    if wt:
        if wt.has_commits():
            log.info("[skill_runner] keeping worktree %s (branch=%s "
                     "has commits → PR)", wt.path, wt.branch)
        else:
            _wt.discard(wt)

    return delta


def reap_orphans(older_than_sec: int = 0) -> list[str]:
    """Lifespan/CLI hook: remove abandoned cto-skill-* containers
    AND un-pushed cto/* worktrees (user never resumed; server
    crashed mid-skill)."""
    out: list[str] = []
    if DockerSandbox.available():
        out += DockerSandbox.reap(SKILL_PREFIX, older_than_sec)
    try:
        out += _wt.reap()
    except Exception as e:
        log.warning("[skill_runner] worktree reap failed: %s", e)
    return out
