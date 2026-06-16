"""
MCP client config loader.

Reads `.mcp.json` from three layered roots (highest precedence first):

  1. <repo>/.mcp.json                (project)
  2. ~/.mcp.json                     (user)
  3. <repo>/data/mcp.json            (bundled defaults)

Expands `${VAR}` against os.environ. A missing required env var
disables that server at load time (warning logged, never crashes).

Output: an ordered dict {name: ServerSpec} ready to be handed to
client.MCPClient. The loader itself does NOT connect to any server.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,40}$", re.IGNORECASE)
_ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


@dataclass(frozen=True)
class ServerSpec:
    """One MCP server entry — parsed + env-interpolated + validated.
    The `connection` dict is whatever langchain_mcp_adapters.sessions
    expects for the resolved transport (Stdio/SSE/StreamableHttp).
    """
    name: str
    transport: str                       # "stdio" | "sse" | "streamable_http"
    connection: dict[str, Any]
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    confirm_writes: bool = True
    subagent_visible: bool = False
    timeout_ms: int = 30_000
    source: str = ""                     # which root provided this

    @property
    def is_stdio(self) -> bool:
        return self.transport == "stdio"


class ConfigError(ValueError):
    pass


def _discovery_roots() -> list[tuple[Path, str]]:
    """[(path, label), …] in precedence order (highest first)."""
    cwd = Path.cwd()
    return [
        (cwd / ".mcp.json", "project"),
        (Path.home() / ".mcp.json", "user"),
        (Path(os.environ.get(
            "MCP_CONFIG_BUNDLED",
            str(Path(__file__).parents[3] / "data" / "mcp.json"))),
         "bundled"),
    ]


def _interpolate(text: str) -> tuple[str, set[str]]:
    """Expand ${VAR}. Returns (expanded, missing_vars). When VAR is
    unset, the placeholder is left intact so the caller can decide
    whether to disable the server."""
    missing: set[str] = set()

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        val = os.environ.get(name)
        if val is None:
            missing.add(name)
            return m.group(0)
        return val

    return _ENV_RE.sub(sub, text), missing


def _interpolate_obj(obj: Any) -> tuple[Any, set[str]]:
    """Recursively interpolate ${VAR} in strings within a JSON-like
    structure. Returns (new_obj, union_of_missing_vars)."""
    if isinstance(obj, str):
        return _interpolate(obj)
    if isinstance(obj, dict):
        out: dict = {}
        missing: set[str] = set()
        for k, v in obj.items():
            nv, m = _interpolate_obj(v)
            out[k] = nv
            missing |= m
        return out, missing
    if isinstance(obj, list):
        out_l: list = []
        missing = set()
        for v in obj:
            nv, m = _interpolate_obj(v)
            out_l.append(nv)
            missing |= m
        return out_l, missing
    return obj, set()


def _infer_transport(raw: dict) -> str:
    forced = raw.get("transport")
    if forced:
        return str(forced).lower().replace("-", "_")
    if "command" in raw:
        return "stdio"
    if "url" in raw:
        # Default HTTP servers to streamable_http (current spec).
        # Legacy SSE-only servers must declare transport: "sse"
        # explicitly.
        return "streamable_http"
    raise ConfigError(
        "server entry has neither `command` (stdio) nor `url` (http)")


def _build_connection(name: str, raw: dict, transport: str
                      ) -> dict[str, Any]:
    """Produce the connection dict accepted by
    MultiServerMCPClient — schema differs per transport."""
    if transport == "stdio":
        if not raw.get("command"):
            raise ConfigError("stdio: `command` is required")
        conn: dict[str, Any] = {
            "transport": "stdio",
            "command": str(raw["command"]),
            "args": [str(a) for a in (raw.get("args") or [])],
        }
        if raw.get("env"):
            # Drop entries whose value is None (missing-var disable).
            env = {k: v for k, v in raw["env"].items() if v is not None}
            conn["env"] = env
        if raw.get("cwd"):
            conn["cwd"] = str(raw["cwd"])
        return conn

    if transport in ("sse", "streamable_http"):
        if not raw.get("url"):
            raise ConfigError(f"{transport}: `url` is required")
        conn = {"transport": transport, "url": str(raw["url"])}
        if raw.get("headers"):
            conn["headers"] = dict(raw["headers"])
        if raw.get("timeout_ms"):
            conn["timeout"] = float(raw["timeout_ms"]) / 1000.0
        return conn

    raise ConfigError(f"unsupported transport: {transport!r}")


def _parse_one(name: str, raw: dict, source: str) -> ServerSpec | None:
    """Parse + validate + interpolate ONE server entry. Returns None
    if the entry is disabled (explicitly or via missing env var)."""
    if not _NAME_RE.match(name):
        raise ConfigError(
            f"invalid server name {name!r} "
            "(alpha-numeric, dot/dash/underscore, ≤40 chars)")
    if not isinstance(raw, dict):
        raise ConfigError("entry must be a mapping")
    if raw.get("enabled") is False:
        log.info("[mcp] %s disabled by config", name)
        return None

    expanded, missing = _interpolate_obj(raw)
    if missing:
        log.warning("[mcp] %s disabled — missing env var(s): %s",
                    name, ", ".join(sorted(missing)))
        return None

    transport = _infer_transport(expanded)
    connection = _build_connection(name, expanded, transport)

    return ServerSpec(
        name=name,
        transport=transport,
        connection=connection,
        allowed_tools=tuple(str(t)
                            for t in (expanded.get("allowed_tools") or [])),
        denied_tools=tuple(str(t)
                           for t in (expanded.get("denied_tools") or [])),
        confirm_writes=bool(expanded.get("confirm_writes", True)),
        subagent_visible=bool(expanded.get("subagent_visible", False)),
        timeout_ms=int(expanded.get("timeout_ms", 30_000)),
        source=source,
    )


def load_config(*, paths: list[tuple[Path, str]] | None = None
                ) -> dict[str, ServerSpec]:
    """Walk the discovery roots, parse `.mcp.json` files, merge into
    {name: ServerSpec}. Higher-precedence roots win on name collision.
    Fail-soft per server: a bad entry logs + skips, never crashes the
    whole load."""
    roots = paths or _discovery_roots()
    out: dict[str, ServerSpec] = {}
    seen_source: dict[str, str] = {}

    for path, label in roots:
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.warning("[mcp] %s (%s): invalid JSON — %s",
                        path, label, e)
            continue
        if not isinstance(raw, dict):
            log.warning("[mcp] %s (%s): top-level must be an object",
                        path, label)
            continue
        servers = raw.get("mcpServers") or raw.get("servers") or {}
        if not isinstance(servers, dict):
            log.warning("[mcp] %s (%s): `mcpServers` must be a map",
                        path, label)
            continue
        for name, entry in servers.items():
            if name in out:
                log.info(
                    "[mcp] %s shadowed by higher-precedence %s "
                    "(skipping %s root)",
                    name, seen_source[name], label)
                continue
            try:
                spec = _parse_one(name, entry, label)
            except ConfigError as e:
                log.warning("[mcp] %s (%s) skipped: %s",
                            name, label, e)
                continue
            if spec is None:
                continue
            out[name] = spec
            seen_source[name] = label

    if out:
        log.info("[mcp] loaded %d server(s): %s",
                 len(out), sorted(out))
    return out
