"""
Playbooks (Phase 8.5-B): methodology prompts for /superdev.

A *playbook* is a markdown file under data/playbooks/ — pure
prose, no frontmatter, no tools. It's prepended to the superdev
system prompt to enforce a workflow (TDD, plan-then-execute,
systematic-debugging). Unlike skills, playbooks don't run in a
sandbox or get their own ReAct loop — they shape how the
existing host+RAG tools are used.

This is the landing zone for ported superpowers-style content:
strip the Agent/Task calls, keep the methodology.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

PLAYBOOKS_DIR = Path(os.environ.get(
    "PLAYBOOKS_DIR",
    str(Path(__file__).parents[3] / "data" / "playbooks")))

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


def list_playbooks() -> list[str]:
    if not PLAYBOOKS_DIR.exists():
        return []
    return sorted(
        p.stem for p in PLAYBOOKS_DIR.glob("*.md")
        if _NAME_RE.match(p.stem))


def load(name: str) -> str | None:
    """Returns the playbook body, or None if not found."""
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        return None
    p = PLAYBOOKS_DIR / f"{name}.md"
    if not p.exists():
        return None
    body = p.read_text(encoding="utf-8").strip()
    log.info("[playbook] loaded %s (%d chars)", name, len(body))
    return body


def render(names: list[str]) -> str:
    """Concatenate one or more playbooks into a single block
    for prepending to the system prompt."""
    parts: list[str] = []
    for n in names:
        body = load(n)
        if body:
            parts.append(f"## Playbook: {n}\n\n{body}")
        else:
            log.warning("[playbook] %r not found in %s "
                        "(have: %s)", n, PLAYBOOKS_DIR,
                        list_playbooks())
    if not parts:
        return ""
    return ("# Active Playbooks\n\n"
            "Follow these workflows STRICTLY — they are not "
            "suggestions. When a playbook step conflicts with "
            "convenience, the playbook wins.\n\n"
            + "\n\n---\n\n".join(parts)
            + "\n\n---\n\n")
