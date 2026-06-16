"""
Host tools (Phase 8-C). NOT in TOOLS_BY_NAME / ALL_TOOLS — these
run subprocess.run(shell=True) directly on the user's machine.
They exist ONLY in the superdev graph's toolset, which is gated
behind CTO_SUPERDEV_ENABLED + local-mode + an explicit /superdev
slash. The read-only graph never imports this module.

Path-jail: write/edit are confined to the session worktree plus
any directory the user added via `/superdev allow <dir>`. The
user trusts *themselves*, not the LLM's argument generation — a
prompt-injected file in the worktree shouldn't be able to
redirect a write to ~/.ssh.

Destructive-pattern detect: host_shell matches a denylist (rm -rf,
dd, mkfs, force-push, sudo, writes to /etc|/usr|~/.ssh|~/.aws) and
surfaces an `ask_user` confirmation before running. That list is
the entire safety model for 8-C — keep it conservative.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path

from langchain_core.tools import tool, BaseTool
from langgraph.types import interrupt

from ...mcp.registry import get_mcp_tools
from ...sandbox.base import sanitize_output
from ...vcs.worktree import Worktree
from ...vcs import pr as _pr
from .jira import build_jira_write_tools

log = logging.getLogger(__name__)

_OUTPUT_CAP = 16_000

# 8.5-A: read-only RAG tools added to the superdev toolset so the
# coding agent can search the INDEXED corpus (all repos, code
# graph, git history) while editing the worktree. One-way import:
# the read-only graph still never imports host.py.
_RAG_TOOL_NAMES = (
    "search", "search_code", "find_symbol", "find_callers", "find_callees",
    "grep", "read_file", "repo_info", "git_log", "git_show",
    "git_blame", "find_commits_for_jira", "search_docs",
    "web_search", "web_fetch",
)


def _rag_tools() -> list[BaseTool]:
    from . import TOOLS_BY_NAME  # lazy: avoid cycle at module import
    return [TOOLS_BY_NAME[n] for n in _RAG_TOOL_NAMES
            if n in TOOLS_BY_NAME]

DESTRUCTIVE = [
    (r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*r[a-zA-Z]*f|"
     r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*f[a-zA-Z]*r|"
     r"\brm\s+-rf\b|\brm\s+-fr\b", "rm -rf"),
    (r"\bdd\b.*\bof=", "dd of="),
    (r"\bmkfs\b|\bfdisk\b|\bparted\b", "filesystem format"),
    (r"git\s+push\b.*(-f\b|--force\b|--force-with-lease\b)",
     "force push"),
    (r"git\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f|checkout\s+--\s)",
     "destructive git"),
    (r"\bsudo\b|\bdoas\b", "sudo"),
    (r":(){:\|:&};:|:\(\)\s*{\s*:\s*\|\s*:\s*&\s*}", "fork bomb"),
    (r"\b(shutdown|reboot|halt|poweroff|init\s+0|init\s+6)\b",
     "shutdown/reboot"),
    (r"\bkill\s+-9\s+-1\b|\bkillall\b", "kill -9 -1 / killall"),
    (r"(curl|wget)\b.*\|\s*(ba)?sh\b", "pipe-to-shell"),
    (r"(>|>>|\btee\b)\s*['\"]?(/etc/|/usr/|/boot/|/sys/|/proc/)",
     "write to system path"),
    (r"~?/\.ssh\b|~?/\.aws\b|~?/\.kube\b|~?/\.docker/config",
     "credential directory"),
    (r"\bchmod\s+-R\s+777\b|\bchown\s+-R\b", "recursive chmod/chown"),
    (r"\bdocker\s+(system\s+prune|rm\s+-f|rmi\s+-f|"
     r"volume\s+(rm|prune))\b", "docker prune/rm -f"),
]
_DESTRUCTIVE_RE = [(re.compile(p, re.IGNORECASE), why)
                   for p, why in DESTRUCTIVE]


def detect_destructive(cmd: str) -> str | None:
    for pat, why in _DESTRUCTIVE_RE:
        if pat.search(cmd):
            return why
    return None


def _jailed(path: str, roots: list[Path]) -> Path | None:
    try:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = roots[0] / p
        rp = p.resolve()
    except (OSError, ValueError):
        return None
    for root in roots:
        try:
            if rp.is_relative_to(root.resolve()):
                return rp
        except (OSError, ValueError):
            continue
    return None


# ── factory ────────────────────────────────────────────────────────

def build_host_tools(wt: Worktree | None,
                     allowed_dirs: list[Path],
                     todo_state: list | None = None,
                     *,
                     child_mode: bool = False,
                     with_commit: bool = True,
                     confirm: callable | None = None
                     ) -> list[BaseTool]:
    """One toolset per superdev session.

    With a worktree (`/superdev <repo>`): host tools + RAG tools
    + glob/patch/todo (8.5). Writes jailed to the worktree.

    Shell-only (`/superdev` no repo): host_shell/read/docker/
    ask_user + RAG tools + todo. No write/edit/glob/patch/
    commit/create_pr — there's no throwaway branch.

    `todo_state` is a caller-owned list mutated in place by the
    `todo` tool so the CLI toolbar can render N/M done.

    `child_mode` (8.5-D subagents): drops ask_user/todo/
    create_pr (parent owns those); `with_commit=False`
    additionally drops commit (sequential children share the
    parent worktree). `confirm` overrides the destructive-gate
    interaction — pass `lambda cmd, why: False` for auto-deny
    (children can't surface interrupt() to the user)."""
    roots = allowed_dirs
    default_cwd = (wt.path if wt else roots[0])
    todos = todo_state if todo_state is not None else []

    def _confirm_destructive(cmd: str, why: str) -> bool:
        if confirm is not None:
            return bool(confirm(cmd, why))
        ans = str(interrupt({
            "kind": "superdev-confirm",
            "question": (
                f"⚠️  host_shell wants to run a command that "
                f"matches a destructive pattern ({why}):\n\n"
                f"  {cmd}\n\nRun it?"),
            "options": ["yes — run it", "no — skip"],
        })).strip().lower()
        return ans.startswith(("y", "yes", "1"))

    @tool
    def host_shell(cmd: str, cwd: str | None = None,
                   timeout: int = 120) -> str:
        """Run a shell command directly on this machine
        (subprocess, shell=True). cwd defaults to the session
        worktree. Output capped at ~16 KB — for large listings,
        use --query/--filter/jq/head to narrow at the source
        instead of post-filtering. Destructive patterns
        (rm -rf, force-push, sudo, writes to /etc|~/.ssh, …) pause
        for user confirmation before running. Returns JSON:
        {exit_code, stdout, stderr, duration_ms, cwd}."""
        why = detect_destructive(cmd)
        if why and not _confirm_destructive(cmd, why):
            return json.dumps({
                "exit_code": 126, "stdout": "", "duration_ms": 0,
                "stderr": (f"[blocked: {why}"
                           + (" — subagents cannot confirm "
                              "destructive ops]" if child_mode
                              else "]")),
                "cwd": cwd,
            })

        run_cwd = str(default_cwd)
        if cwd:
            jp = _jailed(cwd, roots)
            if jp is None or not jp.is_dir():
                return json.dumps({
                    "exit_code": 126, "stdout": "", "duration_ms": 0,
                    "stderr": f"cwd {cwd!r} is outside the allowed "
                              f"roots {[str(r) for r in roots]}",
                    "cwd": cwd})
            run_cwd = str(jp)

        t0 = time.perf_counter()
        try:
            r = subprocess.run(
                cmd, shell=True, cwd=run_cwd, capture_output=True,
                timeout=min(int(timeout), 600))
            return json.dumps({
                "exit_code": r.returncode,
                "stdout": sanitize_output(r.stdout, _OUTPUT_CAP),
                "stderr": sanitize_output(r.stderr, 8192),
                "duration_ms": int((time.perf_counter() - t0) * 1000),
                "cwd": run_cwd,
            })
        except subprocess.TimeoutExpired as e:
            return json.dumps({
                "exit_code": 124,
                "stdout": sanitize_output(e.stdout or b"", _OUTPUT_CAP),
                "stderr": "[timeout exceeded]",
                "duration_ms": int((time.perf_counter() - t0) * 1000),
                "cwd": run_cwd,
            })

    @tool
    def host_read(path: str, max_bytes: int = 200_000) -> str:
        """Read any file on this machine (unrestricted). Relative
        paths resolve against the worktree (or shell-only cwd) —
        same default as host_shell, host_glob, host_write. Returns
        the text content (truncated at max_bytes)."""
        try:
            p = Path(path).expanduser()
            if not p.is_absolute():
                p = default_cwd / p
            data = p.resolve().read_bytes()[:max_bytes]
            return data.decode("utf-8", "replace")
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    @tool
    def host_write(path: str, content: str) -> str:
        """Write/overwrite a file. Path is JAILED to the session
        worktree (and any user-allowed dirs) — writes outside are
        refused. Creates parent dirs as needed."""
        jp = _jailed(path, roots)
        if jp is None:
            return (f"ERROR: {path!r} is outside the worktree/"
                    f"allowed roots. Use `/superdev allow <dir>` "
                    f"to extend.")
        try:
            jp.parent.mkdir(parents=True, exist_ok=True)
            jp.write_text(content, encoding="utf-8")
            return f"Wrote {jp} ({len(content)} bytes)"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    @tool
    def host_edit(path: str, old: str, new: str) -> str:
        """Exact-string replace within a file (path-jailed). Fails
        if `old` doesn't appear exactly once."""
        jp = _jailed(path, roots)
        if jp is None:
            return (f"ERROR: {path!r} is outside the worktree/"
                    f"allowed roots.")
        try:
            text = jp.read_text(encoding="utf-8")
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"
        n = text.count(old)
        if n != 1:
            return (f"ERROR: `old` matched {n}× (need exactly 1). "
                    f"Provide more surrounding context.")
        jp.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"Edited {jp} (Δ {len(new) - len(old):+d} bytes)"

    @tool
    def docker_run(cmd: str, image: str = "python:3.12-slim",
                   network: bool = True, timeout: int = 300) -> str:
        """Run `cmd` inside a Docker container with the worktree
        mounted at /work:rw. Network ON by default — the inverse
        of the Phase-3 sandbox (here YOU chose to isolate; you may
        still want internet). Use this when you don't want a
        command touching the host directly."""
        argv = [
            "docker", "run", "--rm", "-i",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--memory", "2g", "--cpus", "2", "--pids-limit=256",
            *(["--network=none"] if not network else []),
            "-v", f"{default_cwd}:/work:rw", "-w", "/work",
            image, "sh", "-lc", "exec sh -s",
        ]
        t0 = time.perf_counter()
        try:
            r = subprocess.run(
                argv, capture_output=True, timeout=timeout + 10,
                input=cmd.encode("utf-8"))
            return json.dumps({
                "exit_code": r.returncode,
                "stdout": sanitize_output(r.stdout, _OUTPUT_CAP),
                "stderr": sanitize_output(r.stderr, 8192),
                "duration_ms": int((time.perf_counter() - t0) * 1000),
            })
        except subprocess.TimeoutExpired:
            return json.dumps({"exit_code": 124, "stdout": "",
                               "stderr": "[timeout]", "duration_ms":
                               int((time.perf_counter() - t0) * 1000)})
        except FileNotFoundError:
            return json.dumps({"exit_code": 127, "stdout": "",
                               "stderr": "docker not found",
                               "duration_ms": 0})

    @tool
    def ask_user(question: str, options: list[str] | None = None) -> str:
        """Pause and ask the human. Use BEFORE any action you're
        unsure about — in superdev mode, when in doubt, ask."""
        return str(interrupt({
            "kind": "superdev",
            "question": question,
            "options": options or [],
        }))

    @tool
    def host_glob(pattern: str, root: str | None = None) -> str:
        """List files matching a glob pattern under the worktree
        (or a user-allowed dir). Supports `**` for recursive
        matching. Returns one relative path per line, sorted,
        capped at 500 entries. Use this instead of parsing
        `find`/`ls` output."""
        base = roots[0]
        if root:
            jp = _jailed(root, roots)
            if jp is None or not jp.is_dir():
                return f"ERROR: root {root!r} outside allowed dirs"
            base = jp
        try:
            matches = sorted(
                str(p.relative_to(base))
                for p in base.glob(pattern)
                if p.is_file() and ".git" not in p.parts)
        except (ValueError, OSError) as e:
            return f"ERROR: {e}"
        n = len(matches)
        if n > 500:
            matches = matches[:500] + [f"… ({n - 500} more)"]
        return "\n".join(matches) if matches else "(no matches)"

    @tool
    def host_apply_patch(diff: str) -> str:
        """Apply a unified diff to the worktree (multi-file,
        multi-hunk). Paths in the diff are relative to the
        worktree root; every target file is path-jailed before
        applying. Use this for refactors that touch several
        files — emit ONE diff instead of N host_edit calls.
        Returns per-file status. Rolls back nothing on partial
        failure — `git -C <wt> checkout -- <file>` to undo."""
        # Parse target paths from +++ headers and verify jail.
        targets = re.findall(
            r"^\+\+\+\s+(?:b/)?(.+?)\s*$", diff, re.MULTILINE)
        if not targets:
            return "ERROR: no '+++' file headers found in diff"
        for t in targets:
            if t == "/dev/null":
                continue
            if _jailed(t, roots) is None:
                return (f"ERROR: diff target {t!r} is outside "
                        f"the worktree/allowed roots")
        r = subprocess.run(
            ["patch", "-p1", "--no-backup-if-mismatch",
             "--forward", "-d", str(default_cwd)],
            input=diff.encode("utf-8"), capture_output=True,
            timeout=30)
        out = sanitize_output(r.stdout, 4000)
        err = sanitize_output(r.stderr, 4000)
        if r.returncode != 0:
            return (f"ERROR (exit {r.returncode}): {err or out}\n"
                    f"Hint: ensure paths are relative and context "
                    f"lines match exactly.")
        return f"Applied {len(targets)} file(s):\n{out}"

    @tool
    def todo(action: str, item: str = "",
             items: list[str] | None = None) -> str:
        """Maintain a working checklist for the current task.
        Actions:
          set   — replace the list with `items` (all pending)
          add   — append one `item`
          done  — mark `item` (or its 1-based index) complete
          show  — return the current list
        The CLI toolbar shows "N/M done" live. Use `set` once at
        the start of a multi-step task, `done` after each step.
        """
        from langgraph.config import get_stream_writer
        action = action.strip().lower()
        if action == "set":
            todos.clear()
            for it in (items or ([item] if item else [])):
                todos.append({"text": str(it), "done": False})
        elif action == "add" and item:
            todos.append({"text": item, "done": False})
        elif action == "done" and item:
            idx = None
            if item.strip().isdigit():
                idx = int(item.strip()) - 1
            else:
                idx = next((i for i, t in enumerate(todos)
                            if not t["done"]
                            and item.lower() in t["text"].lower()),
                           None)
            if idx is None or not (0 <= idx < len(todos)):
                return f"(no pending item matches {item!r})"
            todos[idx]["done"] = True
        elif action != "show":
            return "ERROR: action must be set|add|done|show"
        n_done = sum(1 for t in todos if t["done"])
        try:
            get_stream_writer()({"skill_event": {
                "kind": "todo", "done": n_done,
                "total": len(todos),
                "items": [dict(t) for t in todos]}})
        except Exception:
            pass
        lines = [f"{'✓' if t['done'] else '·'} {i+1}. {t['text']}"
                 for i, t in enumerate(todos)]
        return (f"[{n_done}/{len(todos)}]\n"
                + "\n".join(lines)) if todos else "(empty)"

    @tool
    def commit(kind: str, area: str, description: str) -> str:
        """Stage all changes in the worktree and commit with the
        enforced `<type>(<area>): <desc>` + Co-Authored-By footer.
        kind ∈ {feat, fix, chore, docs, refactor, test, perf, sec}."""
        try:
            sha = _pr.commit_all(wt, kind, area, description)
            return f"Committed {sha} on {wt.branch}"
        except Exception as e:
            return f"ERROR: {e}"

    jira_writes = build_jira_write_tools(
        confirm=(lambda _a, _s: False) if child_mode else None)
    mcp_tools = get_mcp_tools(
        child_mode=child_mode,
        confirm=(None if child_mode else _confirm_destructive))
    base = [host_shell, host_read, docker_run,
            *_rag_tools(), *jira_writes, *mcp_tools]
    if not child_mode:
        base += [ask_user, todo]
    if wt is None:
        return base

    write_set = [host_write, host_edit, host_glob,
                 host_apply_patch]
    if with_commit:
        write_set.append(commit)

    if child_mode:
        return base + write_set

    @tool("create_pr")
    def create_pr(title: str, body: str = "") -> str:
        """Push this session's worktree branch and open a PR/MR
        against the repo's default branch. Returns the PR URL.
        Use ONCE, after `commit`. You cannot target main/master."""
        if wt.has_changes():
            return ("ERROR: uncommitted changes — `commit` first.")
        try:
            return f"Opened: {_pr.create_pr(wt, title=title, body=body)}"
        except Exception as e:
            return f"ERROR: {e}"

    return base + write_set + [create_pr]
