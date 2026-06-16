"""
Generic SKILLS.md loader (Phase 8.5-E).

Supports the Anthropic-style skill format:

  <skill_root>/<name>/SKILL.md     ← frontmatter + markdown body
  <skill_root>/<name>/scripts/...  ← optional bundled assets
  <skill_root>/<name>/...          ← anything else (referenced by body)

Frontmatter (YAML, between leading `---` fences):

  name: my-skill            (required; lowercase, [a-z0-9-], ≤40 chars)
  description: ...          (required; one-line summary, shown in /skills)
  allowed-tools: [Bash, …]  (optional; parsed but unused in v1 —
                             skills receive the full superdev toolset.
                             Surfaced via Skill.allowed_tools for a
                             future enforcement pass.)
  license: ...              (optional; informational)

Body (after the second `---`) is the system-prompt addendum spliced
into the superdev agent on `/<name>` invocation.

Discovery order (highest precedence wins on name collision):
  1. <repo>/.claude/skills/<name>/SKILL.md     (project-local)
  2. ~/.claude/skills/<name>/SKILL.md          (user-global)
  3. <repo>/data/skills/<name>/SKILL.md        (repo-bundled)

The old custom-format `data/skills/*.md` (single-file, with
`sandbox:`/`triggers:` extensions) is NOT touched here — that loader
remains in `registry.py`. The two coexist; on name collision between
the two systems, the custom-format skill wins (back-compat).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


@dataclass(frozen=True)
class GenericSkill:
    name: str
    description: str
    body: str
    dir: Path                          # parent dir of SKILL.md
    allowed_tools: tuple[str, ...] = ()
    license: str = ""
    source: str = ""                   # which root provided this skill


def _skill_roots() -> list[tuple[Path, str]]:
    """Return [(dir, label), …] in discovery order (highest first)."""
    roots: list[tuple[Path, str]] = []
    cwd = Path.cwd()
    roots.append((cwd / ".claude" / "skills", "project"))
    roots.append((Path.home() / ".claude" / "skills", "user"))
    bundled = Path(os.environ.get(
        "GENERIC_SKILLS_DIR",
        str(Path(__file__).parents[3] / "data" / "skills")))
    roots.append((bundled, "bundled"))
    return roots


def _parse_skill_md(path: Path, source: str) -> GenericSkill:
    raw = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise ValueError(
            "no YAML frontmatter (--- … --- block at top of file)")
    fm_text, body = m.group(1), m.group(2).strip()
    try:
        meta = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        # Common cause: a value containing `:` that wasn't quoted
        # (e.g. `description: Use when: investigating an API`).
        raise ValueError(
            f"invalid YAML in frontmatter — {e}. "
            "Tip: quote values that contain ':' or '#'.") from None
    if not isinstance(meta, dict):
        raise ValueError("frontmatter is not a mapping")

    # Name: prefer frontmatter, fall back to parent dir name.
    name = str(meta.get("name") or path.parent.name).strip()
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid name {name!r} (lowercase, a-z0-9-, ≤40 chars)")

    desc = str(meta.get("description") or "").strip()
    if not desc:
        raise ValueError("description is required (one-line summary)")
    if not body:
        raise ValueError("body (after the second ---) is empty")

    tools_raw = meta.get("allowed-tools") or meta.get("allowed_tools") or []
    if isinstance(tools_raw, str):
        tools_raw = [t.strip() for t in tools_raw.split(",") if t.strip()]
    tools = tuple(str(t).strip() for t in tools_raw)

    return GenericSkill(
        name=name,
        description=desc,
        body=body,
        dir=path.parent,
        allowed_tools=tools,
        license=str(meta.get("license") or "").strip(),
        source=source,
    )


def discover() -> dict[str, GenericSkill]:
    """Walk discovery roots, return {name: GenericSkill}. Earlier
    roots win on name collision. Fail-soft per file."""
    out: dict[str, GenericSkill] = {}
    for root, label in _skill_roots():
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            try:
                sk = _parse_skill_md(skill_md, label)
            except Exception as e:
                log.warning("[generic-skills] %s skipped (%s): %s",
                            skill_md, label, e)
                continue
            if sk.name in out:
                log.info("[generic-skills] %s: shadowed by higher-"
                         "precedence %s (skipping %s root)",
                         sk.name, out[sk.name].source, label)
                continue
            out[sk.name] = sk
    if out:
        log.info("[generic-skills] loaded %d: %s",
                 len(out), sorted(out))
    return out


def get(name: str) -> GenericSkill | None:
    return discover().get(name)


# ── slash parsing ────────────────────────────────────────────────────

_SLASH_RE = re.compile(r"^/([a-z][a-z0-9-]{0,40})(?:\s+(.*))?$",
                       re.DOTALL)


def parse_invocation(text: str) -> tuple[str, str] | None:
    """`/skill-name args…` → ('skill-name', 'args…'). Returns None
    if the leading token isn't a slash command."""
    if not text:
        return None
    m = _SLASH_RE.match(text.strip())
    if not m:
        return None
    return m.group(1), (m.group(2) or "").strip()


def render_body(skill: GenericSkill, args: str) -> str:
    """Substitute $ARGS / $1..$9 placeholders in the skill body
    with the user-provided args string (and whitespace-split
    positional args). Unreferenced positionals are simply ignored;
    referenced positionals beyond the supplied count expand to ''."""
    parts = args.split() if args else []
    rendered = skill.body.replace("$ARGS", args)
    for i in range(1, 10):
        rendered = rendered.replace(
            f"${i}", parts[i - 1] if i - 1 < len(parts) else "")
    return rendered


# Generic-skill tool names → superdev host-tool names. The skill body
# is a free-form prompt; the LLM is smart enough to map most names,
# but giving it an explicit table prevents ambiguity for the common
# Anthropic-convention names (Bash / Read / Write / Edit / Glob /
# Grep / WebFetch / WebSearch / Task / TodoWrite / AskUserQuestion).
_TOOL_NAME_HINTS = (
    ("Bash", "host_shell"),
    ("Read", "host_read / read_file"),
    ("Write", "host_write"),
    ("Edit", "host_edit"),
    ("MultiEdit", "host_apply_patch"),
    ("Glob", "host_glob"),
    ("Grep", "grep"),
    ("WebFetch", "web_fetch"),
    ("WebSearch", "web_search"),
    ("Task", "spawn / spawn_parallel"),
    ("TodoWrite", "todo"),
    ("AskUserQuestion", "ask_user"),
)


def compose_user_message(skill: GenericSkill, args: str) -> str:
    """Build the user-message replacement that the superdev agent
    sees when the user types `/<name> <args>`. The skill body is
    rendered (placeholder substitution) and wrapped with a header
    so the agent knows which skill is active, where its bundled
    assets live, and how generic tool names map to the superdev
    host tools that actually exist in this graph."""
    rendered = render_body(skill, args)
    head = [
        f"# Skill activated: {skill.name}",
        f"_{skill.description}_",
        "",
        f"Bundled assets: `{skill.dir}` "
        "(absolute path — pass to host_shell / host_read directly).",
    ]
    if args:
        head.append(f"User arguments: `{args}`")

    # Tool-name aliasing: only mention names the skill actually
    # asks for (or, if it didn't declare any, mention all common ones).
    declared = set(skill.allowed_tools)
    declared_base = {re.split(r"[\s(]", t, 1)[0] for t in declared}
    relevant = [(g, h) for g, h in _TOOL_NAME_HINTS
                if not declared_base or g in declared_base]
    if relevant:
        head += ["",
                 "Tool name mapping (generic → host-tool):"]
        head += [f"  - {g} → {h}" for g, h in relevant]

    head += ["", "---", "", rendered]
    return "\n".join(head)


def maybe_splice(messages: list) -> tuple[list, GenericSkill | None]:
    """If the latest message is a HumanMessage containing
    `/<skill-name> [args]` and the skill is loadable, return a NEW
    messages list with the last entry replaced by the composed
    skill prompt + the resolved Skill. Otherwise return the
    original list and None. Pure / side-effect-free (caller does
    the logging)."""
    from langchain_core.messages import HumanMessage  # lazy: avoid cycle
    if not messages or not isinstance(messages[-1], HumanMessage):
        return messages, None
    inv = parse_invocation(str(messages[-1].content))
    if not inv:
        return messages, None
    sk = get(inv[0])
    if sk is None:
        return messages, None
    out = list(messages)
    out[-1] = HumanMessage(content=compose_user_message(sk, inv[1]))
    return out, sk
