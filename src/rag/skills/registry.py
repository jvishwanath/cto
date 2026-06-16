"""
Skill registry (Phase 7-E).

A *skill* is a markdown file under data/skills/: YAML frontmatter
declaring how to run it (triggers, sandbox mode/image/limits,
max_iter, allowed tools) + a body that becomes the system prompt.
The registry loads them at startup, fail-soft (a malformed file
logs and is skipped — never crashes the server), and exposes
match()/get()/list_skills() for the router, slash commands, and
skill_runner.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

SKILLS_DIR = Path(os.environ.get(
    "SKILLS_DIR", str(Path(__file__).parents[3] / "data" / "skills")
))

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


@dataclass(frozen=True)
class SandboxCfg:
    mode: str = "ephemeral"          # "ephemeral" | "persistent" | "none"
    image: str = "cto-sandbox:latest"
    timeout: int = 60                # per-command (run/exec)
    run_timeout: int = 1800          # whole-container wall clock (persistent)
    network: bool = False
    memory: str = "1g"
    # Phase 8-D: mount a host-side git worktree at /work:rw instead
    # of tmpfs. Implies persistent. Enables create_pr.
    worktree: bool = False
    # Phase 9-B: cred passthrough for ops skills. Allowlisted env
    # vars are forwarded as -e K; mounts as extra -v src:dst:ro.
    env_passthrough: tuple[str, ...] = ()
    mounts: tuple[str, ...] = ()

    def __post_init__(self):
        if self.worktree and self.mode != "persistent":
            raise ValueError(
                "sandbox.worktree: true requires sandbox.mode: "
                "persistent")
        if self.mode not in ("ephemeral", "persistent", "none"):
            raise ValueError(
                f"sandbox.mode must be ephemeral|persistent|none, "
                f"got {self.mode!r}")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    triggers: tuple[str, ...] = ()
    sandbox: SandboxCfg = field(default_factory=SandboxCfg)
    max_iter: int = 30
    tools_allowed: tuple[str, ...] = ()
    path: Path | None = None

    @property
    def needs_shell(self) -> bool:
        return "run_shell_command" in self.tools_allowed


# ── module-level cache ─────────────────────────────────────────────

_SKILLS: dict[str, Skill] = {}
_TRIGGERS: list[tuple[re.Pattern, str]] = []
_loaded = False


def _parse(path: Path) -> Skill:
    raw = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise ValueError("no YAML frontmatter (--- … --- block)")
    fm_text, body = m.group(1), m.group(2).strip()
    meta = yaml.safe_load(fm_text) or {}
    if not isinstance(meta, dict):
        raise ValueError("frontmatter is not a mapping")

    name = str(meta.get("name") or path.stem).strip()
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid name {name!r} (lowercase, a-z0-9-, ≤40 chars)")

    sb = meta.get("sandbox") or {}
    if not isinstance(sb, dict):
        raise ValueError("sandbox: must be a mapping")

    triggers = tuple(str(t) for t in (meta.get("triggers") or []))
    # Validate each regex compiles — catch typos at load time.
    for t in triggers:
        re.compile(t)

    tools = tuple(str(t) for t in (meta.get("tools_allowed") or []))
    if not body:
        raise ValueError("body (after the second ---) is empty")

    return Skill(
        name=name,
        description=str(meta.get("description") or "").strip(),
        body=body,
        triggers=triggers,
        sandbox=SandboxCfg(
            mode=str(sb.get("mode", "none" if not any(
                t == "run_shell_command" for t in tools)
                else "ephemeral")),
            image=str(sb.get("image", SandboxCfg.image)),
            timeout=int(sb.get("timeout", SandboxCfg.timeout)),
            run_timeout=int(sb.get("run_timeout",
                                   SandboxCfg.run_timeout)),
            network=bool(sb.get("network", False)),
            memory=str(sb.get("memory", SandboxCfg.memory)),
            worktree=bool(sb.get("worktree", False)),
            env_passthrough=tuple(
                str(e) for e in (sb.get("env_passthrough") or [])),
            mounts=tuple(
                str(m) for m in (sb.get("mounts") or [])),
        ),
        max_iter=int(meta.get("max_iter", 30)),
        tools_allowed=tools,
        path=path,
    )


def load_skills(*, refresh: bool = False) -> dict[str, Skill]:
    """Glob data/skills/*.md → {name: Skill}. Fail-soft per file."""
    global _SKILLS, _TRIGGERS, _loaded
    if _loaded and not refresh:
        return _SKILLS

    out: dict[str, Skill] = {}
    triggers: list[tuple[re.Pattern, str]] = []
    if SKILLS_DIR.exists():
        for f in sorted(SKILLS_DIR.glob("*.md")):
            try:
                sk = _parse(f)
            except Exception as e:
                log.warning("[skills] %s skipped: %s", f.name, e)
                continue
            if sk.name in out:
                log.warning("[skills] duplicate name %r in %s — "
                            "keeping %s", sk.name, f.name,
                            out[sk.name].path.name)
                continue
            out[sk.name] = sk
            for t in sk.triggers:
                triggers.append((re.compile(t, re.IGNORECASE),
                                 sk.name))

    _SKILLS = out
    _TRIGGERS = triggers
    _loaded = True
    log.info("[skills] loaded %d skill(s) from %s: %s",
             len(out), SKILLS_DIR, sorted(out))
    return _SKILLS


def get(name: str) -> Skill | None:
    return load_skills().get(name)


def match(query: str) -> Skill | None:
    """Router hook: first skill whose trigger regex matches the
    query, or None. Skills with no triggers are slash-only."""
    load_skills()
    for pat, name in _TRIGGERS:
        if pat.search(query):
            return _SKILLS.get(name)
    return None


def list_skills() -> list[Skill]:
    return sorted(load_skills().values(), key=lambda s: s.name)


# ── auto-scaffold (used by the watcher) ───────────────────────────

_HOST_TOOL_NAMES = {
    "run_shell_command": "run_shell_command",
    "grep_search": "grep_search", "grep": "grep_search",
    "read_file": "read_file", "search_code": "search_code",
    "find_symbol": "find_symbol", "find_callers": "find_callers",
    "find_callees": "find_callees", "repo_info": "repo_info",
    "git_log": "git_log", "git_blame": "git_blame",
    "git_show": "git_show", "jira_lookup": "jira_lookup",
    "jira_search": "jira_search", "write_report": "write_report",
    "ask_user": "ask_user", "write_todos": "write_todos",
    "search_docs": "search_docs",
}
# Conservative default for a body that doesn't reveal what it
# needs: read-only RAG tools + report + ask_user. NO run_shell.
_DEFAULT_TOOLS = (
    "repo_info", "read_file", "grep_search", "search_code",
    "find_symbol", "find_callers", "git_log",
    "write_report", "ask_user",
)


def has_frontmatter(text: str) -> bool:
    return _FRONTMATTER_RE.match(text) is not None


def scaffold_frontmatter(path: Path) -> bool:
    """If `path` has no frontmatter, prepend a minimal one with
    safe defaults + TODO markers and rewrite the file. Returns
    True if it scaffolded, False if it already had frontmatter
    (or isn't readable). Idempotent.

    Defaults are deliberately conservative: no triggers (slash-
    only), read-only tools (no run_shell_command), sandbox.mode
    auto-derives to 'none'. The skill loads and is invokable via
    /<name>; the user tunes triggers/tools/sandbox by hand."""
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return False
    if has_frontmatter(raw):
        return False

    stem = path.stem.lower()
    name = re.sub(r"[^a-z0-9-]+", "-", stem).strip("-") or "skill"
    if not _NAME_RE.match(name):
        name = re.sub(r"^[^a-z]+", "", name)[:40] or "skill"

    # Heuristic tool detection: if the body mentions a known tool
    # name, include it. Always union with the safe defaults.
    body_lower = raw.lower()
    detected = sorted({
        cto for hint, cto in _HOST_TOOL_NAMES.items()
        if hint in body_lower
    } | set(_DEFAULT_TOOLS))
    needs_shell = "run_shell_command" in detected

    # First non-empty line as a one-line description.
    desc = next(
        (ln.lstrip("# ").strip() for ln in raw.splitlines()
         if ln.strip() and not ln.strip().startswith("---")),
        "",
    )[:140]

    fm_lines = [
        "---",
        f"name: {name}",
        f"description: {desc or '(TODO: one-line description)'}",
        "# TODO: add natural-language triggers (regex). Without",
        "#       these the skill is /slash-only.",
        "triggers: []",
        "max_iter: 40",
        "# TODO: review tools_allowed — this is the capability",
        "#       boundary. Detected from the body + safe defaults.",
        "tools_allowed:",
    ]
    fm_lines += [f"  - {t}" for t in detected]
    if needs_shell:
        fm_lines += [
            "# run_shell_command detected — review sandbox config:",
            "sandbox:",
            "  mode: ephemeral   # TODO: persistent if calls chain state",
            "  image: cto-secaudit:latest",
            "  timeout: 300",
            "  network: false    # TODO: true if runtime installs needed",
        ]
    fm_lines += ["---", ""]

    # If the body never mentions write_report, inject a runtime-
    # notes block so the agent checkpoints output incrementally
    # (avoids the max_tokens truncation we hit when a skill tries
    # to emit a 13-section report as one final answer). Placed
    # immediately after frontmatter so it's the first instruction
    # the LLM reads, ahead of the original playbook.
    runtime_block = ""
    if "write_report" not in body_lower:
        runtime_block = "\n".join([
            "> **CTO runtime notes (auto-inserted — keep or adapt):**",
            "> - Save your output incrementally: after each major",
            ">   section, call",
            f'>   `write_report(repo, "<today>-{name}.md", '
            'content="<section markdown>", append=True)`',
            ">   (use `append=False` only for the first section,",
            ">   prefixed with the report title). Keep each section",
            ">   under ~3 KB.",
            "> - When all sections are written, return ONLY a 1–2",
            ">   line summary (saved path + headline result). Do",
            ">   NOT re-emit the full report as your final answer.",
            "> - You cannot delete, move, or modify repo files —",
            ">   list such actions as recommendations in the report.",
            "",
            "",
        ])

    path.write_text("\n".join(fm_lines) + runtime_block + raw,
                    encoding="utf-8")
    log.info("[skills] scaffolded frontmatter for %s "
             "(name=%s, %d tools%s%s)",
             path.name, name, len(detected),
             ", needs_shell" if needs_shell else "",
             ", +runtime-notes" if runtime_block else "")
    return True
