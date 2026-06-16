"""
Skill-specific tool primitives (Phase 7-C/D).

These are NOT in agents/tools/ALL_TOOLS — they're built per-skill by
`build_skill_tools()` so the sandbox config (image/timeout/network/
cid) is closed over and the LLM can only pass the command/content.

  run_shell_command — built per skill via make_run_shell_tool();
                      dispatches to ephemeral run_shell() or
                      persistent exec(cid, …) by sandbox.mode.
  write_report      — path-jailed write under data/reports/<repo>/.
  ask_user          — wraps interrupt(); verified to work directly
                      inside a tool with create_react_agent +
                      PostgresSaver (Phase-7 plan, 7-D).
  write_todos       — no-op stub so source skills that call it
                      don't error.
  grep_search       — alias of the regular grep tool, named to
                      match host-agent skill conventions.

`build_skill_tools()` assembles the final list from a Skill's
`tools_allowed`, drawing from both this module and the existing
agents/tools.TOOLS_BY_NAME.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.tools import tool, BaseTool
from langgraph.types import interrupt

from ..sandbox import DockerSandbox
from ..agents.tools import TOOLS_BY_NAME
from ..agents.tools.grep import grep as _grep_impl
from ..vcs.worktree import Worktree
from ..vcs import pr as _pr

if TYPE_CHECKING:
    from .registry import Skill

log = logging.getLogger(__name__)

REPORTS_DIR = Path(os.environ.get(
    "REPORTS_DIR", str(Path(__file__).parents[3] / "data" / "reports")
))
_REPORT_MAX = 2 * 1024 * 1024  # 2 MB
_FNAME_RE = re.compile(r"^[\w.\- ]{1,120}$")


# ── run_shell (built per skill — closes over sandbox config) ──────

def make_run_shell_tool(
    *,
    mode: str,
    image: str,
    timeout: int,
    network: bool,
    repo: str | None,
    cid: str | None = None,
    sandbox: DockerSandbox | None = None,
) -> BaseTool:
    """Factory: returns a `run_shell_command` @tool whose body has
    image/timeout/network/repo/cid baked in. The LLM only supplies
    `command` — it cannot escalate sandbox config."""
    sb = sandbox or DockerSandbox()
    persistent = (mode == "persistent")

    if persistent and not cid:
        raise ValueError("persistent mode requires cid (call "
                         "DockerSandbox.start() in skill_runner first)")

    note = (
        "All calls share ONE container for this run — env, cwd, "
        "installed packages, and files under /work persist across "
        "calls. /repo is read-only; copy to /work before mutating."
        if persistent else
        "Each call is a FRESH container — nothing persists between "
        "calls. Write self-contained one-shot commands. /work is a "
        "writable tmpfs; /repo is read-only."
    )

    @tool("run_shell_command")
    def _run_shell(command: str) -> str:
        """Run a shell command in the skill sandbox.

        The repo is mounted read-only at /repo; /work is writable
        scratch. Returns JSON: {{exit_code, stdout, stderr,
        duration_ms, timed_out}}. stdout is truncated to ~50KB —
        prefer `--json`/`--quiet` flags and filter output (head/jq)
        for large scans. {note}"""
        if not DockerSandbox.available():
            return json.dumps({"exit_code": 127, "timed_out": False,
                               "stdout": "", "duration_ms": 0,
                               "stderr": "Docker is not available on "
                                         "this host — run_shell_command "
                                         "is disabled."})
        if persistent:
            if not DockerSandbox.alive(cid):
                return json.dumps({"exit_code": 125, "timed_out": False,
                                   "stdout": "", "duration_ms": 0,
                                   "stderr": f"sandbox container {cid} "
                                             f"is not running (timed out "
                                             f"or reaped) — cannot exec."})
            r = sb.exec(cid, command, timeout=timeout)
        else:
            r = sb.run_shell(command, repo=repo, image=image,
                             timeout=timeout, network=network)
        return json.dumps({
            "exit_code": r.exit_code,
            "timed_out": r.timed_out,
            "duration_ms": r.duration_ms,
            "stdout": r.stdout,
            "stderr": r.stderr,
        })

    # Splice the mode-specific note into the docstring the LLM sees.
    _run_shell.description = _run_shell.description.format(note=note)
    return _run_shell


# ── write_report (shared, path-jailed) ────────────────────────────

@tool
def write_report(repo: str, filename: str, content: str = "",
                 append: bool = False) -> str:
    """Write a markdown report to data/reports/<repo>/<filename>.

    Use this for the skill's final deliverable. The path is jailed
    to the reports directory — `..`, absolute paths, and subdirs are
    rejected. Max 2 MB. Returns the saved relative path on success.
    The report is auto-indexed into the docs collection so follow-up
    questions ("what were the CRITICAL findings?") can retrieve it.

    For long reports, you may call this multiple times with
    `append=True` to write section-by-section instead of emitting
    the whole document in one tool call.
    """
    if not content:
        return ("ERROR: `content` is empty or was truncated. If the "
                "report is large, write it in sections with "
                "`append=True`, or emit a shorter version. Retry "
                "with the full markdown body in `content`.")
    repo = (repo or "").strip().strip("/")
    if not repo or not re.fullmatch(r"[\w.\-]{1,80}", repo):
        return f"ERROR: invalid repo name {repo!r}"
    if not filename or not _FNAME_RE.fullmatch(filename):
        return (f"ERROR: invalid filename {filename!r} — use a flat "
                f"name like 'YYYY-MM-DD-security-audit.md' (no /, "
                f"no .., max 120 chars)")
    if len(content) > _REPORT_MAX:
        return (f"ERROR: content too large ({len(content)} bytes > "
                f"{_REPORT_MAX}). Trim or split.")

    out_dir = REPORTS_DIR / repo
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    # Belt-and-suspenders: resolve and verify containment.
    try:
        if not path.resolve().is_relative_to(REPORTS_DIR.resolve()):
            return "ERROR: path escapes reports directory"
    except (ValueError, OSError):
        return "ERROR: path resolution failed"

    if append and path.exists():
        with path.open("a", encoding="utf-8") as f:
            f.write("\n" + content)
    else:
        path.write_text(content, encoding="utf-8")
    rel = f"reports/{repo}/{filename}"
    total = path.stat().st_size
    log.info("[write_report] %s (%s%d → %d bytes)",
             rel, "+ " if append else "", len(content), total)

    # Best-effort: index into the docs collection so it's searchable.
    try:
        from ..ingest.docs import index_doc
        index_doc(path)
    except Exception as e:
        log.warning("[write_report] index_doc(%s) failed: %s", rel, e)

    return f"Saved: {rel} ({len(content):,} bytes)"


# ── ask_user (interrupt() — verified in-tool with PostgresSaver) ───

def make_ask_user_tool(headless: bool = False,
                       headless_answers: dict | None = None
                       ) -> BaseTool:
    """Factory: in interactive mode, `interrupt()`. In headless
    (Phase 9-C scheduled runs), return the job's configured
    answer (longest-substring match against headless_answers
    keys) or 'skip', and never block."""
    answers = headless_answers or {}

    @tool("ask_user")
    def ask_user(question: str,
                 options: list[str] | None = None) -> str:
        """Pause and ask the human a question. Blocks until they
        reply.

        Use SPARINGLY — only when you genuinely cannot proceed
        without a decision (e.g., "install <X>? it'll take ~3
        min", "fix CRITICAL only or CRITICAL+HIGH?"). The reply
        string is returned verbatim. `options` is shown as a
        numbered list; the user may pick one or type free text.
        """
        if headless:
            for key, val in answers.items():
                if key.lower() in question.lower():
                    log.info("[ask_user headless] %r → %r "
                             "(matched %r)", question[:60],
                             val, key)
                    return str(val)
            log.info("[ask_user headless] %r → 'skip' "
                     "(no headless_answers match)",
                     question[:60])
            return "skip"
        return str(interrupt({
            "kind": "skill",
            "question": question,
            "options": options or [],
        }))

    return ask_user


ask_user = make_ask_user_tool()


# ── stubs / aliases for host-agent skill conventions ──────────────

@tool
def write_todos(items: list[str] | str) -> str:
    """Track a checklist (no-op in CTO — the agent's own context is
    the checklist). Returns a confirmation so the call doesn't error.
    """
    n = len(items) if isinstance(items, list) else 1
    return f"(tracked {n} item{'s' if n != 1 else ''})"


_BRE_TO_ERE = re.compile(r"\\([|()+?{}])")


@tool("grep_search")
def grep_search(pattern: str, repo: str | None = None,
                file_glob: str | None = None) -> str:
    """Search for a regex pattern across the repo's files (ripgrep).

    The pattern is an EXTENDED regex — use `|` for alternation
    (NOT `\\|`), and `.` `+` `?` `()` are metacharacters by
    default. `file_glob` accepts `**` for recursive matching.
    """
    # Host-agent skills (and Claude trained on bash grep) often emit
    # BRE-style `\|`, `\(`, `\+` — which ripgrep treats as LITERAL.
    # Translate to ERE so 'A\|B' works as the model intended.
    ere = _BRE_TO_ERE.sub(r"\1", pattern)
    return _grep_impl.invoke(
        {"pattern": ere, "repo": repo, "file_glob": file_glob})


# ── create_pr (Phase 8-B; built per worktree — opt-in only) ───────

def make_create_pr_tool(wt: Worktree) -> BaseTool:
    """Factory: bind `create_pr` to a single worktree so the LLM
    supplies title/body only — it cannot choose the branch, repo,
    or push target. Not in _SKILL_STATIC_TOOLS; only added when a
    worktree exists for this run (8-D) or in the superdev toolset
    (8-C)."""

    @tool("create_pr")
    def _create_pr(title: str, body: str = "") -> str:
        """Open a pull/merge request from this run's worktree
        branch against the repo's default branch. Returns the PR
        URL. Use this ONCE, after you've committed your changes
        under /work and the build passes. The branch is fixed; you
        cannot push to or target main/master directly."""
        if wt.has_changes():
            return ("ERROR: uncommitted changes in the worktree — "
                    "commit (or revert) before opening a PR.")
        try:
            url = _pr.create_pr(wt, title=title, body=body)
        except Exception as e:
            return f"ERROR: {e}"
        return f"Opened: {url}"

    return _create_pr


# ── per-skill tool assembly ────────────────────────────────────────

# Tools that exist independent of sandbox config — same instance
# every skill. (run_shell_command is NOT here; it's built per-call.)
_SKILL_STATIC_TOOLS: dict[str, BaseTool] = {
    "write_report": write_report,
    "ask_user": ask_user,
    "write_todos": write_todos,
    "grep_search": grep_search,
}


def build_skill_tools(
    skill: "Skill",
    repo: str | None,
    cid: str | None = None,
    worktree: Worktree | None = None,
    headless: bool = False,
    headless_answers: dict | None = None,
) -> list[BaseTool]:
    """Assemble the narrowed toolset for one skill run.

    For each name in skill.tools_allowed:
      - 'run_shell_command'      → factory-built, closes over sandbox cfg
      - in _SKILL_STATIC_TOOLS   → that tool
      - in agents/TOOLS_BY_NAME  → the regular RAG tool (read_file,
                                   search_code, git_log, …)
      - else                     → warn + skip (skill md typo)

    A skill that doesn't list run_shell_command CANNOT execute
    shell — that's the capability boundary, enforced here, not by
    the prompt.
    """
    out: list[BaseTool] = []
    seen: set[str] = set()
    for name in skill.tools_allowed:
        if name in seen:
            continue
        seen.add(name)
        if name == "create_pr":
            if worktree is None:
                log.warning("[skill %s] tools_allowed lists create_pr "
                            "but no worktree was started "
                            "(sandbox.worktree: true) — skipped",
                            skill.name)
                continue
            out.append(make_create_pr_tool(worktree))
        elif name == "run_shell_command":
            out.append(make_run_shell_tool(
                mode=skill.sandbox.mode,
                image=skill.sandbox.image,
                timeout=skill.sandbox.timeout,
                network=skill.sandbox.network,
                repo=repo, cid=cid,
            ))
        elif name == "ask_user":
            out.append(make_ask_user_tool(headless,
                                           headless_answers))
        elif name in _SKILL_STATIC_TOOLS:
            out.append(_SKILL_STATIC_TOOLS[name])
        elif name in TOOLS_BY_NAME:
            out.append(TOOLS_BY_NAME[name])
        else:
            log.warning("[skill %s] unknown tool %r in tools_allowed "
                        "— skipped", skill.name, name)
    return out


def default_report_name(skill_name: str) -> str:
    return f"{date.today().isoformat()}-{skill_name}.md"
