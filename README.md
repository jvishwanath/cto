# CTO — Code · Trace · Origin

> **From signal to source — cited to the line.**
>
> CTO is three products in one toolchain:
>
> 1. **An agentic advanced RAG system** over your
>    internal code, docs, wikis, and live web — dense + sparse
>    vector retrieval, cross-encoder reranking, AST-aware
>    chunking, call-graph traversal, cascaded multi-tier
>    routing, evaluator-driven self-correction.
> 2. **An agentic coding assistant** — `/superdev` runs a
>    LangGraph ReAct agent on your machine with file
>    read/write/edit, shell, commit, and PR tools, jailed to a
>    git worktree with destructive-action confirmation gates
>    and parallel subagent spawn.
> 3. **A sandboxed execution + skills platform** — pluggable
>    Markdown-defined workflows execute inside hardened Docker
>    sandboxes (ephemeral `--rm --read-only --network=none` or
>    persistent with scoped credentials) so untrusted code and
>    third-party tooling never touch the host.

[![phase](https://img.shields.io/badge/phase-10-blue)](docs/PHASE10_ARCHITECTURE.md)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![langgraph](https://img.shields.io/badge/langgraph-0.2%2B-purple)](https://github.com/langchain-ai/langgraph)
[![license](https://img.shields.io/badge/license-internal-lightgrey)](#license)

---

## Why this exists

Search-engine LLMs hallucinate against your private code.
Generic RAG bolts a vector DB onto your repos and stops at
"semantic chunk retrieval" — useful for a description, useless
for *"what calls this function across our microservices, and
which JIRAs touched it last quarter?"* And neither lets an
agent actually **fix** what it finds.

CTO is built around three product pillars:

### 1. Agentic hybrid advanced RAG

Not vector-search-plus-prompt — a retrieval pipeline that an
LLM agent drives:

- **Hybrid retrieval** — dense embeddings (`text-embedding-3
  -large` @ 1024d) **+** sparse term-frequency vectors, fused
  natively in Qdrant (RRF)
- **Cross-encoder reranking** — `bge-reranker-v2-m3` re-scores
  the fusion's top-K so cosine-similarity false positives drop
  before they reach the model
- **AST-aware code chunking** — `tree-sitter` parsers for
  Java/Python/JS/TS/Go/Rust/C++/HCL produce semantic chunks
  with class/function boundaries instead of arbitrary windows
- **Call-graph traversal** — Postgres recursive CTEs over
  symbol + edge tables answer `find_callers` / `find_callees`
  exactly, with case-insensitive matching
- **Cascaded multi-source routing** — `code → local docs →
  Confluence → web` with early-exit at the first strong-enough
  tier; agent picks the right scope automatically
- **Self-correcting agent loop** — LangGraph `StateGraph` with
  per-query intent classification, optional clarification
  interrupt, evaluator (CRAG retrieval grading + LLM-judge
  answer grading) with retry-with-critique up to 2×
- **Parallel deep-research subgraph** — `Send`-based fan-out
  over decomposed sub-questions for broad/comparative queries
- **Provenance enforcement** — every claim cites `[SOURCE_N]`,
  footnotes verified, abstain on weak retrieval

### 2. Agentic coding assistant

Not just answering questions — actually doing the work, safely:

- **`/superdev` host-write agent** — ReAct loop with
  `host_shell`, `host_read`, `host_write`, `host_edit`,
  `host_apply_patch`, `host_glob`, `commit`, `create_pr`
- **Worktree isolation** — every superdev session opens a git
  worktree under `data/superdev/<session>/<repo>` on a
  throwaway `cto/*` branch; the source clone stays pristine;
  writes path-jailed to the worktree (+ user-allowed dirs)
- **Destructive-action gates** — `host_shell` denylist
  (rm -rf, force-push, sudo, writes to `/etc`/`~/.ssh`/…)
  pauses for human confirmation via LangGraph `interrupt()`
- **Parallel subagent spawn** — `spawn_parallel(tasks)` forks
  N children on isolated worktrees, cherry-picks commits back
  on success
- **Composable methodology playbooks** — `tdd`,
  `systematic-debugging`, `plan-then-execute`,
  `verify-before-done`, `subagent-driven` (drop-in additional
  system-prompt prefixes)
- **PR-only writes** — the agent never pushes to main; every
  change lands as a PR/MR you review

### 3. Sandboxed execution + skills platform

Untrusted code and third-party scanners need somewhere safe to
run. Skills are pluggable Markdown workflows that execute
inside hardened Docker containers:

- **Two sandbox modes** — `ephemeral` (`--rm --read-only
  --network=none --cap-drop=ALL --security-opt=
  no-new-privileges`) for one-shot scanners; `persistent`
  (named container, scoped network + mounts, lifecycle-managed)
  for discover-then-install workflows
- **Pre-baked scanner image** — `cto-secaudit` ships with
  semgrep, trivy, gitleaks, tfsec, checkov, bandit, pip-audit,
  osv-scanner plus node/go/jdk/cargo toolchains
- **Capability-gated tools** — `tools_allowed` in skill
  frontmatter is enforced by `build_skill_tools()` — a skill
  that doesn't list `run_shell_command` cannot shell,
  regardless of prompt content
- **Scoped credential passthrough** — `env_passthrough` +
  `mounts` allowlists let scheduled ops jobs use AWS/GCP
  CLIs in a container without exposing them to anything else
- **Path-jailed reports** — `write_report` confines outputs to
  `data/reports/<repo>/`, auto-indexes for follow-up retrieval
- **In-band `ask_user` interrupts** — sandboxed skills can
  surface clarification prompts mid-run without breaking the
  isolation boundary

Plus the surrounding production system:

- **Schedulable everything** — cron + interval jobs over
  skills, queries, or arbitrary `exec` scripts; Slack
  notifications; diff-since-last hooks; HTML dashboard
- **Full observability** — Arize Phoenix tracing on every
  turn (OpenTelemetry / OpenInference auto-instrumentation),
  one click from CLI/UI output to the span tree
- **MCP client** — consumes filesystem, GitHub, memory,
  fetch, sequential-thinking, and any other MCP server you
  configure
- **Multi-interface** — REST + SSE (FastAPI), Gradio web UI,
  Rich-based terminal REPL, single-file native binary CLI

---

## Quick start

### End user (binary, recommended)

```bash
# 1. Download binary from your team's artifact share
curl -O https://your-org.internal/cto-darwin-arm64
chmod +x cto-darwin-arm64
xattr -d com.apple.quarantine cto-darwin-arm64   # macOS Gatekeeper

# 2. Run the wizard
./cto-darwin-arm64 setup

# 3. Verify
./cto-darwin-arm64 doctor

# 4. Ask something
./cto-darwin-arm64 "how is JWT validation handled?"
./cto-darwin-arm64   # → REPL
```

### Developer (Python install)

```bash
git clone <repo-url>
cd cto
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Bring up local infra (qdrant + postgres + phoenix)
make infra

# Index your repos
mkdir -p data/repos
git clone <your-repo> data/repos/my-repo
make index-all

# Chat
make chat
```

---

## Architecture

```mermaid
flowchart LR
    subgraph clients[Clients]
      CLI["cto binary<br/>(CLI / TUI)"]
      UI["Gradio Web UI"]
      API["REST + SSE<br/>(FastAPI)"]
    end

    subgraph server[CTO Server]
      direction TB
      RG["Regular Graph<br/>(read-only)"]
      SG["Superdev Graph<br/>(host write +<br/>worktree + PR)"]
      SCH["Scheduler<br/>(cron skills)"]
    end

    subgraph stores[Stores]
      QDRANT[(Qdrant<br/>dense + sparse)]
      PG[(Postgres<br/>checkpointer + store<br/>code graph + jobs)]
      PHX[(Phoenix<br/>OTel traces)]
    end

    subgraph external[External]
      LLM["LLM Gateway<br/>(LiteLLM proxy →<br/>Anthropic / OpenAI / etc)"]
      CONF["Confluence"]
      JIRA["JIRA"]
      WEB["Web search<br/>(Tavily / DDG)"]
      GH["GitHub / GitLab<br/>(PRs)"]
      MCP["MCP servers<br/>(filesystem / github /<br/>memory / fetch / ...)"]
    end

    CLI --> server
    UI --> server
    API --> server

    RG --> QDRANT
    RG --> PG
    SG --> QDRANT
    SG --> PG
    SCH --> SG
    SCH --> RG

    server --> PHX
    server --> LLM
    server --> CONF
    server --> JIRA
    server --> WEB
    server --> GH
    SG --> MCP
```

### Query flow (regular mode)

```mermaid
flowchart TD
    Q([User query]) --> R[reset_turn]
    R --> IG{input_guard<br/>PII redact +<br/>jailbreak detect}
    IG -->|blocked| END1([abstain])
    IG -->|continue| LM[load_memories]
    LM --> QA[query_analysis<br/>intent + scope]
    QA --> CL{clarify?}
    CL -->|needs human| INT[interrupt → ask user]
    CL -->|continue| ROUTE{router}
    INT --> ROUTE
    ROUTE -->|simple| SR[simple_rag<br/>single-shot]
    ROUTE -->|agent| AL[agent_loop<br/>ReAct]
    ROUTE -->|research| DR[deep_research<br/>fan-out subgraph]
    ROUTE -->|intro| IN[intro static]
    ROUTE -->|skill| SK[skill_runner sandbox]
    SR --> EV{evaluator}
    AL --> EV
    DR --> EV
    EV -->|low score, retry| AL
    EV -->|pass| OG[output_guard<br/>cite verify +<br/>secret scrub]
    OG --> RS[respond]
    IN --> RS
    SK --> RS
    RS --> SUM{summarize?}
    SUM -->|yes| S[compress history]
    SUM -->|no| SM[save_memories]
    S --> SM
    SM --> END([answer + citations])
```

### Superdev mode (coding agent)

```mermaid
flowchart TD
    Q([User query in /superdev]) --> R[reset]
    R --> IG{input_guard}
    IG -->|blocked| END1
    IG -->|continue| LM[load_memories]
    LM --> SL[superdev_loop<br/>ReAct over:<br/>host_shell + host_edit +<br/>commit + RAG tools +<br/>MCP + JIRA writes]
    SL --> TC{classify turn}
    TC -->|read| EV[evaluator]
    TC -->|action| SK[skip]
    EV --> OG[output_guard]
    SK --> OG
    OG --> RS[respond] --> SUM{summarize?}
    SUM --> SM[save_memories<br/>read turns only]
    SM --> END([answer + tool trace])
```

Phase 8.5-G splices the regular graph's quality stages around
`superdev_loop` while keeping two graph builders — see
[docs/PHASE8_5G_ARCHITECTURE.md](docs/PHASE8_5G_ARCHITECTURE.md).

---

## Features

### Retrieval — cascaded, multi-source

| Tier | Source | Tool | When |
|---|---|---|---|
| 1 | Code chunks (tree-sitter AST) | `search_code` | Code-shaped queries |
| 2 | Local markdown / PDF docs | `search_docs` | Doc-shaped queries |
| 3 | Confluence pages | (cascaded into `search`) | Tier 1+2 weak |
| 4 | Web search | `web_search`, `web_fetch` | Out-of-corpus |
| Graph | Code call graph (Postgres CTE) | `find_symbol`, `find_callers`, `find_callees` | Exact-name lookups |
| History | Git log / blame / show | `git_log`, `git_show`, `git_blame` | Change-history Qs |
| Tickets | JIRA REST | `jira_lookup`, `jira_search`, `find_commits_for_jira` | Ticket refs |

Cascade short-circuits at the first tier with strong-enough hits.

### Agent loops

- **Simple RAG** — single-shot for direct lookups
- **ReAct agent loop** — hand-rolled tool-calling loop; ~18 tools
- **Deep research subgraph** — `Send`-based fan-out over sub-questions; recombines findings
- **Superdev** — host-write-capable ReAct loop; worktree-jailed; subagent spawn + cherry-pick

### Quality gates

- **Input guard** — PII redaction, jailbreak/off-topic block
- **Evaluator** — CRAG retrieval grading + LLM-judge answer grading; retry up to 2× with refined prompts
- **Output guard** — citation footnote verification; secret scrub; abstain on weak retrieval
- **Semantic cache** — Qdrant-backed; mode-scoped (`regular` vs `superdev`); TTL 1h

### Skills + playbooks

**Skills** (`data/skills/*.md`) — pluggable workflows the
router can invoke via `/skill-name` or trigger regex. Each
has a sandbox config (ephemeral docker / persistent / none),
allowed tools, max iterations, and a markdown body that
becomes the system prompt.

Bundled:
- `security-audit` — semgrep + trivy + gitleaks + checkov in a `cto-secaudit` container; can edit + PR
- `onboarding-tour` — guided walkthrough
- `cve-check`, `api-explore`, `eli5` — examples
- `project-cleanup`, `caveman` — playbook-style

**Playbooks** (`data/playbooks/*.md`) — methodology prefixes
that prepend to the superdev system prompt. Compose like
mixins: `/superdev my-repo tdd verify-before-done`.

Bundled: `tdd`, `systematic-debugging`, `plan-then-execute`,
`verify-before-done`, `subagent-driven`.

### MCP integration

CTO is an MCP client. Configure servers in `.mcp.json` —
filesystem, github (PAT-authed), memory, sequential-thinking,
fetch, the "everything" reference server. Tools, resources,
and prompts all surface to the agent. Capability probe at
startup; servers that fail to spawn are skipped (fail-soft).

### Scheduler

`scheduler/runner.py` — daemon-thread scheduler for cron and
interval jobs. Three kinds:
- `skill:` — invoke a skill headlessly
- `query:` — fire a query against the regular graph
- `exec:` — run a script in `cto-ops` sandbox (or host, with
  env_passthrough gate)

Notifications: `slack://` webhook, `log://`, diff-since-last
hooks. `GET /jobs` HTML dashboard + Gradio modal + CLI
`/jobs`. Schedule definitions in `data/schedules.yaml`.

### Observability

Arize Phoenix (OpenTelemetry/OpenInference) — single
container, auto-instrumentation for LangChain + LangGraph.
Every turn emits a span tree with model calls, retrieval,
evaluator scores. `🔍 trace` link in CLI/UI output. `make
trace` opens dashboard.

---

## Two graphs, one codebase

| Aspect | Regular graph | Superdev graph |
|---|---|---|
| Module | [`src/rag/agents/graph.py`](src/rag/agents/graph.py) | [`src/rag/agents/superdev_graph.py`](src/rag/agents/superdev_graph.py) |
| Capability | Read-only over indexed corpus | Read + host shell + worktree write + commit + PR |
| Entry | `cto chat` / `POST /query` | `/superdev` slash command |
| Sandbox | None (no host access) | Path-jailed to worktree + user-allowed dirs |
| Stages | reset → guard → memories → analysis → clarify → router → (simple\|agent\|research\|intro\|skill) → eval → guard → respond → summarize → save | reset → guard → memories → superdev_loop → eval (read turns) → guard → respond → summarize → save |
| Code sharing | — | Imports the regular graph's node functions; only wiring differs |

The wrapper-not-unification design (Phase 8.5-G) keeps two
buildable graphs while reusing all the quality nodes —
extending one extends both.

---

## Storage layout

| Store | Used for | How |
|---|---|---|
| **Qdrant** | Dense + sparse vector search; semantic cache | 1024-dim cosine (text-embedding-3-large); hand-rolled BM25 vocab |
| **Postgres (pgvector)** | LangGraph checkpointer; LangGraph Store (long-term memory); code symbols + edges; scheduled_runs | Per-thread checkpoints; per-user_id memory namespace; recursive CTEs for call graph |
| **Phoenix** | OTel trace store | SQLite per-container; UI at :6006 |
| **Filesystem** | Repos, hf-cache, reports, worktrees, scheduler logs | `data/` and `hf-cache/` bind-mounts |

---

## Configuration

End users: `cto setup` walks you through it (writes XDG-
compliant config + 0600 credentials file).

Developers: copy `.env.example` to `.env`. Notable knobs:

| Env | Default | Purpose |
|---|---|---|
| `LITELLM_URL` | (required) | LLM gateway URL |
| `LITELLM_API_KEY` | (required) | Gateway bearer |
| `QDRANT_URL` | `http://localhost:6333` | Vector DB |
| `POSTGRES_DSN` | `postgresql://rag:rag@localhost:5432/rag` | Checkpointer + store |
| `PHOENIX_HOST` | `http://localhost:6006` | Trace UI |
| `CTO_API_KEYS` | (off) | Comma-separated SHA256'd bearer keys for the API |
| `CTO_TRUST_FORWARDED_USER` | `false` | Honor `X-Forwarded-User` from upstream proxy (oauth2-proxy) |
| `CTO_SUPERDEV_ENABLED` | `false` | Gate for the host-write graph |
| `CTO_REMOTE` | — | Point thin-client CLI at a remote server |
| `JIRA_TOKEN`, `JIRA_BASE_URL` | — | JIRA integration |
| `CONFLUENCE_TOKEN`, `CONFLUENCE_BASE_URL` | — | Confluence indexing |
| Plus | many flags per feature | See `.env.example` |

---

## CLI reference

```
cto                          REPL (default)
cto "<question>"             one-shot
cto setup                    interactive wizard
cto doctor [--unsafe]        connectivity + perms diagnostic
cto compose pull             pull infra images
cto compose up               start qdrant + postgres + phoenix
cto compose down [-v]        stop (-v wipes volumes)
cto compose ps               service status
cto compose logs [svc] [-f]  tail logs
cto compose status           one-shot health probe
cto chat ...                 explicit chat form
```

In-REPL slash commands:

```
/superdev [repo] [playbook ...]    enter coding mode
/exit-superdev                     leave
/superdev allow <dir>              extend write jail
/playbook <name>                   attach playbook mid-session
/playbooks                         list available
/skills                            list available skills
/<skill-name> [args]               invoke skill
/jobs                              scheduler dashboard
/help                              full slash list
```

---

## Project structure

```
src/rag/
├── api/              REST (FastAPI), Gradio UI, CLI REPL
├── cli/              cto setup / doctor / compose (Phase 10)
├── agents/
│   ├── graph.py             Regular graph builder
│   ├── superdev_graph.py    Coding-agent graph builder
│   ├── nodes/               Pipeline nodes (reused across graphs)
│   ├── tools/               Tool implementations
│   └── subgraphs/           Deep research subgraph
├── chunking/         tree-sitter + markdown chunkers
├── connectors/       Confluence connector + watchers
├── embed/            Embedding wrappers
├── full_index/       Full indexer
├── indexing/         Incremental indexer + graph_db
├── mcp/              MCP client (config, client, tools, resources, prompts)
├── memory/           cache, checkpointer, store
├── retrieval/        Hybrid search, rerank, cascade, graph_query
├── sandbox/          Docker sandbox runtime
├── scheduler/        Cron-style job runner
├── skills/           Skill registry + generic SKILL.md loader
├── tracing/          Phoenix/OTel wiring
└── vcs/              Worktrees + PR creation

data/
├── repos/            Indexed source repos (gitignored)
├── skills/           Skill markdown files
├── playbooks/        Methodology prefixes
├── schedules.yaml    Scheduler job definitions
├── reports/          Skill output reports
└── ...

docs/                 Per-phase architecture diagrams + this README
packaging/            PyInstaller spec + bundled compose
tests/                Smoke + pytest suites
```

---

## Build the binary

macOS-first; Linux/Windows later.

```bash
pip install ".[binary]"             # adds pyinstaller>=6.0
make binary                         # native arch (host)
make binary-mac-arm64               # enforced on Apple Silicon
make binary-mac-x86_64              # enforced on Intel
make binary-clean

ls -lh dist/cto-*
# → dist/cto-darwin-arm64  (~30-50 MB single file)
```

PyInstaller does not cross-compile. Build each arch on its
native host. See [packaging/README.md](packaging/README.md).

---

## License


---

## Acknowledgments

Built on the shoulders of:

- **LangGraph** — state graph orchestration with checkpointing + HITL
- **LiteLLM** — multi-provider gateway
- **Qdrant** — vector store with hybrid + sparse support
- **tree-sitter** — language-agnostic AST parsing
- **Arize Phoenix** — OTel-native LLM observability
- **PyInstaller** — Python-to-binary packaging
- **MCP (Model Context Protocol)** — standardized tool/resource servers
- **Anthropic Claude** — primary agent model (Sonnet/Opus 4.x family)

---

> *Code · Trace · Origin — from signal to source, cited to the line.*
