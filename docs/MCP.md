# MCP (Model Context Protocol) — client support

Agentic-rag is an MCP **client**: it consumes tools / resources /
prompts exposed by external MCP servers (filesystem, github, sqlite,
sentry, sequential-thinking, memory, fetch, etc.) and surfaces them
inside the `/superdev` graph as additional tools alongside the native
host toolset.

The read-only Q&A graph (`graph.py`) does NOT consume MCP. MCP is a
coding-agent capability multiplier, not a Q&A retrieval surface.

---

## v1 scope

| Feature | Status |
|---|---|
| MCP tools (call) | ✓ |
| MCP resources (`list_resources` / `read_resource` as tools) | ✓ |
| MCP prompts (rendered as generic skills, slash-invokable) | ✓ |
| Progress notifications (streamed via `skill_event`) | ✓ |
| Cancellation propagation | ✓ |
| Tool list updates (`notifications/tools/list_changed`) | ✓ |
| `initialize` capability handshake | ✓ (via `langchain-mcp-adapters`) |
| stdio transport | ✓ |
| HTTP / SSE transport | ✓ |
| Static bearer auth (`Authorization: Bearer ${TOKEN}`) | ✓ |
| Per-server allowlist / denylist | ✓ |
| Subagent opt-in (default off) | ✓ |
| Destructive-gate wrap around mutating tools | ✓ (default on, opt-out per server) |
| Tool name prefixing (`<server>__<tool>`) | ✓ |
| Resources caching (30 s TTL, invalidated on `list_changed`) | ✓ |
| Layered config (`<repo>/.mcp.json` > `~/.mcp.json` > `data/mcp.json`) | ✓ |
| Env-var interpolation (`${VAR}`; missing var → server disabled, warn) | ✓ |
| App-wide singleton pool (lifespan-managed) | ✓ |

---

## v1 non-scope (deferred — track here)

### Operational

| Item | Why deferred | Notes for revival |
|---|---|---|
| **stdio MCPs in containerized deploy** | App container ships without `node` / `uvx` / `docker` CLI. Local dev (no container) gets full stdio support; EC2 deploy is HTTP/SSE-only. | When needed: add an `mcp-proxy` sidecar service in `docker-compose.yml` that spawns stdio servers and exposes them as HTTP. App talks to the sidecar via HTTP transport. ~0.5 day. Alternative: bake node/uvx into app Dockerfile (+400 MB image, docker.sock mount required for docker-based MCPs). |
| **Per-session pool (multi-tenant)** | Currently single-tenant in practice: shared env-var credentials, one team. App-wide singleton amortizes 1-2 s handshake cost. | When multi-tenant: switch to **hybrid** — HTTP servers stay shared (read-only by convention), stdio servers spawn per session for per-user state + auth. ~0.5 day. Per-user OAuth tokens require the OAuth tier first. |
| **Auto-reconnect on disconnect** | stdio MCPs rarely crash; SSE rarely drops. Cost-benefit thin for v1. | When needed: supervisor with exponential backoff. ~80 LOC. |
| **`mcp-list` / `mcp-status` Make targets** | Phase 5 polish, may slip. | `make mcp-list` enumerates connected servers + tool counts; `make mcp-status` shows healthy / last-seen. ~30 LOC. |

### Spec features

| Item | Why deferred | When to add |
|---|---|---|
| **OAuth 2.1 flow** | Static PAT covers GitHub / GitLab / Sentry / Slack / Brave / Tavily / AWS already. Linear cloud / Notion / Atlassian Cloud are OAuth-only — locked out for v1. | When a user explicitly asks for an OAuth-only server. ~250 LOC: callback HTTP server, token cache (persisted to Postgres alongside checkpoints), refresh flow, per-server auth state. |
| **Sampling** (server → client LLM completion) | Niche; agent-orchestration MCPs (e.g. sequential-thinking variants) use it. | When first server we want needs it. ~150 LOC. Reuse `llm()` factory. |
| **Roots** (client → server workspace hints) | Filesystem MCP can scope itself; without roots, must pre-configure paths in `.mcp.json` args. Works, just clunkier. | Trivial when needed. ~30 LOC. |
| **Elicitation** (server asks user for input mid-call) | Newer spec; few servers use it. Maps cleanly to existing `interrupt()` / `ask_user` infra. | When first server uses it. ~80 LOC. |
| **Resource subscriptions** (`resources/subscribe`) | Server notifies on resource change — useful for watch-style MCPs (filesystem). | When needed. ~60 LOC. Invalidate the 30s resource cache on the notification. |
| **Completion** (autocomplete for prompt / resource args) | CLI-only UX hint; irrelevant for agent flow. | Probably never. |
| **Server logging** (`notifications/message`) | Server-side debug logs not surfaced anywhere. | Route to project logger when first MCP server reveals a useful debug stream. ~20 LOC. |
| **Pagination** (cursor-based for huge tool / resource lists) | Most servers have < 50 tools / few hundred resources. | Edge case. ~30 LOC. |

---

## Config schema (`.mcp.json`)

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
      "env": {}
    },
    "github": {
      "command": "docker",
      "args": ["run", "-i", "--rm",
               "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
               "ghcr.io/github/github-mcp-server"],
      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}
    },
    "remote": {
      "url": "https://mcp.example.com/sse",
      "headers": {"Authorization": "Bearer ${MCP_REMOTE_TOKEN}"},
      "transport": "sse"
    }
  }
}
```

Per-server optional fields:

| Field | Default | Effect |
|---|---|---|
| `allowed_tools: [str]` | null (all) | Restrict which tools the agent sees |
| `denied_tools: [str]` | [] | Drop specific tools (works with `allowed_tools`) |
| `confirm_writes` | `true` | Wrap mutating tools in destructive-gate |
| `subagent_visible` | `false` | Whether `spawn`/`spawn_parallel` children see this server |
| `timeout_ms` | `30000` | Per-call timeout |
| `enabled` | `true` | Disable without removing the config block |
| `transport` | inferred | Force `stdio` / `http` / `sse` |

Discovery layered (highest precedence first):
1. `<repo>/.mcp.json` (project)
2. `~/.mcp.json` (user)
3. `data/mcp.json` (bundled)

Env-var interpolation: `${VAR}` expanded against `os.environ`. Missing var
→ that server is disabled at load time with a warning.

---

## Tool naming

MCP tools are prefixed `<server>__<tool>` (double underscore) so they
never collide with native tools (`host_shell`, `search_code`, etc.).
On collision with a native name, MCP loses and logs a warning.

---

## Security posture

- MCP server commands run with the app process's full perms — config
  is **trusted input**. Treat `.mcp.json` like a Makefile.
- Subprocess MCPs inherit the app's env (after `.mcp.json` `env:`
  overrides). Don't add servers you don't control.
- Subagent isolation: MCP tools default to NOT being visible to
  `spawn`-launched subagents — mirrors how JIRA writes and `ask_user`
  are gated. Opt in per-server via `subagent_visible: true`.
- Destructive-gate: mutating tools (name matches
  `(create|update|delete|write|set|push|merge|kill)` OR server
  declares them as writes) pause for user confirmation via
  `interrupt()` before running. Opt out per-server with
  `confirm_writes: false`.
