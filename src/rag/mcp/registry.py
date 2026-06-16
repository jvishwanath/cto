"""
MCP registry — public API the rest of the codebase wires against.

`get_mcp_tools(child_mode, confirm)` returns a flat list of
BaseTools ready to splice into the superdev toolset:
  * per-server `subagent_visible` honored when child_mode=True
  * per-server `allowed_tools` / `denied_tools` applied
  * mutating tools wrapped in `confirm` when the server opts in
    (server.confirm_writes AND parent mode AND name looks mutating)
  * `<server>__<tool>` prefix applied last so all output names
    are unambiguous

`get_mcp_prompts()` is a Phase-3 placeholder.
"""

from __future__ import annotations

import logging
from typing import Callable

from langchain_core.tools import BaseTool

from . import prompts as _prompts
from . import resources as _resources
from . import tools as _tools
from .client import caps, is_enabled, specs, tools_cache

log = logging.getLogger(__name__)


def get_mcp_tools(*, child_mode: bool = False,
                  confirm: Callable[[str, str], bool] | None = None
                  ) -> list[BaseTool]:
    """Return MCP-backed BaseTools for the current superdev session.
    Empty list when MCP is disabled, no servers loaded, or all
    servers filtered out by child_mode.

    Composes three sources per server:
      1. Native MCP tools (filtered + optionally confirm-wrapped)
      2. Resources surfaced as `list_resources` + `read_resource`
         (when the server's resources capability was detected)
      3. Prompts surfaced as `list_prompts` + `use_prompt` (when
         the server's prompts capability was detected)

    Everything is prefixed `<server>__<tool>` last so all output
    names are unambiguous to the LLM."""
    if not is_enabled():
        return []
    cache = tools_cache()
    if not cache:
        return []
    # Reach into the module-level client singleton; resource/prompt
    # tool factories close over it for session(). When client is
    # absent (e.g. in unit tests that pre-seed only the cache),
    # native tools still flow through; resource/prompt wrappers
    # are skipped.
    from . import client as _client_mod
    client = _client_mod._client

    out: list[BaseTool] = []
    cap_map = caps()
    for name, spec in specs().items():
        if child_mode and not spec.subagent_visible:
            continue
        per_server: list[BaseTool] = []

        # (1) native tools
        per_server.extend(_tools.filter_allowed(
            cache.get(name) or [],
            spec.allowed_tools, spec.denied_tools))

        # (2) + (3) resource / prompt tools (capability-gated;
        # also need a live client to close over).
        if client is not None:
            cap = cap_map.get(name, {})
            if cap.get("resources"):
                per_server.extend(
                    _resources.build_resource_tools(name, client))
            if cap.get("prompts"):
                per_server.extend(
                    _prompts.build_prompt_tools(name, client))

        for t in per_server:
            wrapped = t
            if (not child_mode and spec.confirm_writes
                    and confirm is not None
                    and _tools.looks_mutating(t)):
                wrapped = _tools.wrap_confirm(wrapped, name, confirm)
            out.append(_tools.prefix(wrapped, name))
    return out


def get_mcp_prompts() -> list:
    """Return MCP prompts as generic_skills.GenericSkill objects so
    the existing /skills splice path picks them up.

    Phase-3 fills this in.
    """
    return []
