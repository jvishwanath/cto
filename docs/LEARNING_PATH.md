# Agentic RAG — Interactive Learning Path

> **Goal:** Build a production agentic RAG system over internal code repos, wikis, and live web — while deeply learning LangGraph, multi-agent, memory, and sandboxing.
>
> **Stack:** Python · LangGraph · LiteLLM · Qdrant · tree-sitter · LlamaIndex (ingestion only) · E2B
>
> **Sources:** 10–100 GitLab repos, Confluence wikis, markdown docs, live web search
>
> **Interfaces:** REST API (FastAPI/SSE), Slack, MCP server for Claude Code
>
> **How to use:** Start any phase by saying `"Phase N — let's discuss"` or `"Phase N — alternatives for X"`. Every phase has discussion hooks — explore before committing.

---

## Per-Phase Documentation Convention

At the end of each phase, write `docs/PHASE<N>_ARCHITECTURE.md` containing Mermaid diagrams for:

1. **System Architecture** — components and their connections at this phase
2. **Code/Data Flow** — flowchart (indexing) or sequence diagram (query/agent loop)
3. **Module Dependency Graph** — which files import which
4. **Data Anatomy** — structure of the core artifact (Qdrant point, LangGraph state, graph schema, etc.)
5. **Phase N-1 vs N comparison table** — what changed and measured impact

Completed: `PHASE0`–`PHASE4_6`, `PHASE5`, `PHASE6`, `PHASE7`, `PHASE8`, `PHASE8_5`, `PHASE8_5G`, `PHASE9`, `PHASE10`. Pending: `PHASE11`+ (Sprint 1 skills hardening is planned in `SPRINT1_SKILLS_HARDENING.md`).

---

## Architecture Overview

```
  Sources                     Ingestion                 Index Store
  ───────                     ─────────                 ───────────
  GitLab repos ──webhook──▶   ChunkerRegistry           Qdrant (dense + BM25)
  Confluence   ──webhook──▶     ├─ code.py (tree-sitter) Postgres (code graph)
  Markdown     ──fswatch──▶     ├─ markdown.py (headers) Postgres (LangGraph ckpt + store)
                                └─ (pluggable)

  Interfaces                  Agentic Orchestrator (LangGraph StateGraph)
  ──────────                  ─────────────────────────────────────────
  Slack ─────────────────▶    START → router (Haiku via LiteLLM)
  REST API ──────────────▶         ├─ simple_rag (single-shot)
  MCP Server ◀───────────▶        └─ agent_loop (ReAct w/ tools)
                                        ├─ search_code, search_docs
                                        ├─ read_file, grep
                                        ├─ find_callers, find_callees
                                        ├─ web_search (Tavily)
                                        ├─ execute_code → CodeExecutor subgraph (E2B)
                                        └─ deep_research → fan-out subgraph
                                   → evaluator (quality gate)
                                   → respond (inline citations)
                              → END

  LLM Access: All via LiteLLM proxy (OpenAI-compatible gateway)
  ─────────────────────────────────────────────────────────────
  App → LiteLLM → { Anthropic, Bedrock, Voyage, Cohere, ... }
  One client, one URL. Provider swap = YAML edit.
```

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Chunking | Per-source (tree-sitter for code, header-hierarchy for docs) | Code has hard semantic boundaries; splitting mid-function is fatal |
| Embedding | Single model (voyage-code-3) via LiteLLM; separate only if eval demands | One vector space = cross-source queries work |
| Retrieval | Hybrid (dense + BM25 via RRF) + cross-encoder reranker | Code needs keyword exact-match; concepts need semantic |
| Orchestrator | Single agent with tools (NOT per-source agents) | Cross-source reasoning, low latency, debuggable |
| Deep research | Planner → parallel fan-out subgraph (per-source agents) | Breadth queries benefit from parallelism |
| Multi-agent | Supervisor/worker subgraphs (CodeExecutor, DeepResearch) | Isolation + different loop bounds justify separation |
| Memory | LangGraph Checkpointer (short-term) + Store (long-term) + semantic cache | Framework-native; no custom session store |
| LLM access | LiteLLM proxy service | Provider-agnostic, fallbacks, cost tracking, one client |
| Citations | Inline `[SOURCE_N]` forced by system prompt | Per-sentence auditability; developers need exact file:line |
| State returns | Return deltas only from nodes (not full state) | Correct LangGraph convention; reducers merge properly |
| LlamaIndex | Ingestion primitives only (node parsers, vector store wrapper) | Good chunkers; don't use its agents/query-engines |

---

## Tech Stack & Alternatives

> Legend: 🟢 free/OSS · 💰 paid/managed · ⭐ best-in-class · ✅ our pick · ⚠️ caveat · ❌ avoid for this use case

| Layer | ✅ Our Pick | Alternatives | Notes |
|---|---|---|---|
| **Vector DB** | Qdrant 🟢⭐ | pgvector 🟢 (one DB, weaker hybrid) · Weaviate 🟢 · Milvus 🟢 · Pinecone 💰⭐ (zero-ops) · Chroma 🟢 (⚠️ dev-only) · Elasticsearch 🟢 (heavy) | Qdrant: native hybrid + RRF, great payload filters, easy self-host |
| **Embeddings** | text-embedding-3-large 💰 (via gateway) | voyage-code-3 💰⭐ (best for code) · bge-m3 🟢⭐ (local, dense+sparse) · jina-v3 🟢 · nomic-embed 🟢 · gemini-embedding 💰 | Picked because already in corp gateway; voyage if available |
| **Reranker** | bge-reranker-v2-m3 🟢⭐ (local) | Cohere rerank-3 💰⭐ · jina-reranker 🟢 · LLM-as-reranker 💰 (⚠️ slow) · ❌ none (precision drops) | Local cross-encoder, no API dependency |
| **Code parsing** | tree-sitter 🟢⭐ | SCIP/LSIF 🟢⭐ (⚠️ needs build) · LSP servers 🟢 (⚠️ heavy) · regex (❌ fragile) · LlamaIndex CodeSplitter 🟢 | tree-sitter: 40+ langs, no compile step, fast |
| **Code graph store** | Postgres + recursive CTE 🟢 | Neo4j 🟢⭐ (Cypher, purpose-built) · NetworkX 🟢 (⚠️ in-memory only) · Memgraph 🟢 · Neptune 💰 | Postgres: already running, CTEs sufficient at our scale |
| **Orchestration** | LangGraph 🟢⭐ | Raw SDK 🟢 (⚠️ DIY checkpointer/fan-out) · LlamaIndex agents 🟢 (⚠️ less control) · CrewAI 🟢 · AutoGen 🟢 · Haystack 🟢 | Checkpointer + Send + interrupt are the wins |
| **LLM gateway** | LiteLLM (corp proxy) 🟢⭐ | Direct provider SDKs (⚠️ N clients) · OpenRouter 💰 · Portkey 💰 · Kong/Envoy (⚠️ generic) | One client, fallbacks, cost tracking, provider swap = YAML |
| **Agent LLM** | Claude Sonnet 4.6 💰⭐ | Claude Opus 4.7 💰⭐ (heavy) · GPT-5.x 💰 · Gemini 3.x Pro 💰 · Qwen3-30B 🟢 (⚠️ local, weaker tool-use) | Best tool-use reliability for ReAct loops |
| **Router LLM** | gpt-4.1-mini 💰 | Claude Haiku 4.5 💰 · Gemini Flash 💰 · Llama4 🟢 · regex-only 🟢 (⚠️ brittle) | Cheapest capable; we layer regex first anyway |
| **Conversation memory** | PostgresSaver 🟢 | RedisSaver 🟢 (⚠️ eviction) · MemorySaver 🟢 (❌ not durable) · SQLiteSaver 🟢 (single-node) | Durable, queryable, same DB as graph |
| **Long-term memory** | LangGraph PostgresStore 🟢 *(Phase 4)* | Mem0 🟢 · Zep 💰 · Letta 🟢 · DIY pgvector 🟢 | Framework-native, namespaced by user |
| **Sandbox** | Docker (`--network=none` etc.) 🟢 | E2B 💰⭐ (Firecracker, hosted) · gVisor 🟢⭐ (stronger than Docker) · Modal 💰 · Daytona 💰 · ❌ subprocess (no isolation) | Docker for dev; gVisor/E2B for prod |
| **Web search tool** | Tavily 💰 (free tier) | Brave Search API 💰 · SerpAPI 💰 · Exa 💰⭐ (semantic) · DuckDuckGo 🟢 (⚠️ rate-limited) | LLM-optimized result format |
| **API server** | FastAPI + sse-starlette 🟢⭐ | Flask 🟢 (⚠️ async weak) · Litestar 🟢 · LangServe 🟢 (⚠️ LangChain-coupled) | Async-native, SSE built-in |
| **Tracing/observability** | Phoenix (Arize) 🟢⭐ *(Phase 4.6)* | Langfuse 🟢⭐ (⚠️ v3 needs ClickHouse+Redis+MinIO) · LangSmith 💰⭐ · Helicone 💰 · raw OpenTelemetry 🟢 | Single container, OTel-native, auto-instruments LangChain |
| **PDF extraction** | unstructured 🟢 *(Phase 5)* | PyMuPDF 🟢⭐ (fast) · Marker 🟢⭐ (⚠️ GPU) · pypdf 🟢 (❌ loses structure) · LlamaParse 💰 | `strategy="auto"` handles scanned + text |
| **Wiki connector** | atlassian-python-api 🟢 *(Phase 5)* | Confluence REST direct 🟢 · LlamaHub loader 🟢 | Handles pagination + auth |
| **Queue** | Redis Streams 🟢 (dev) / SQS 💰 (prod) *(Phase 5)* | RabbitMQ 🟢 · Kafka 🟢 (⚠️ overkill) · Pub/Sub 💰 · Celery 🟢 | Simple, KEDA-scalable in prod |
| **Container orchestration** | EKS 💰 *(Phase 6)* | GKE 💰 · AKS 💰 · ECS Fargate 💰 (simpler) · k3s 🟢 · Docker Compose 🟢 (dev) | Standard k8s; Fargate if you want less ops |
| **IaC** | Terraform 🟢⭐ *(Phase 6)* | Pulumi 🟢 (real langs) · CDK 🟢 (AWS-only) · Crossplane 🟢 | Industry default |

### When to switch from our pick

| Trigger | Switch to |
|---|---|
| >1M chunks or >100 QPS vector search | Pinecone or Qdrant Cloud (managed) |
| Need 99% call-graph accuracy (refactoring tool) | Add SCIP indexers per language alongside tree-sitter |
| Graph queries get complex (5+ hop, weighted paths) | Neo4j |
| Running untrusted *user* code in prod | gVisor (`--runtime=runsc`) or E2B |
| Need fully offline (air-gapped) | bge-m3 embeddings + Qwen3-30B local LLM via Ollama |
| >10k concurrent chat sessions | RedisSaver (with AOF for durability) |
| Confluence/PDF retrieval quality poor on long docs | RAPTOR tree-index or Marker for PDF→md |

---

## Dev Environment

All local via `docker compose up`:

| Service | Image | Port | Purpose |
|---|---|---|---|
| Qdrant | `qdrant/qdrant` | 6333 | Vectors + BM25 |
| Postgres | `postgres:16` | 5432 | Code graph + LangGraph checkpointer/store + LiteLLM spend |
| Redis | `redis:7-alpine` | 6379 | Queue (webhook → indexer) |
| LiteLLM | `ghcr.io/berriai/litellm` | 4000 | LLM gateway |
| Phoenix | `arizephoenix/phoenix` | 6006 | Tracing (OTel collector + UI) |

Resource footprint: ~4GB RAM. Fits on 16GB laptop.

---

## Repo Layout

```
cto/
├── docker-compose.yml
├── litellm_config.yaml
├── .env.example
├── Makefile                    # dev, reindex, sync-repos, eval, mcp
├── pyproject.toml
├── src/rag/
│   ├── config.py               # ENVIRONMENT={dev,prod}, factory for all clients
│   ├── connectors/
│   │   ├── base.py             # Connector protocol
│   │   ├── gitlab.py
│   │   └── confluence.py
│   ├── chunking/
│   │   ├── registry.py         # ChunkerRegistry keyed by extension/MIME
│   │   ├── code.py             # tree-sitter AST chunker
│   │   └── markdown.py         # header-hierarchy chunker
│   ├── indexing/
│   │   ├── embedder.py         # calls LiteLLM /v1/embeddings
│   │   ├── vector_store.py     # Qdrant client wrapper
│   │   ├── code_graph.py       # Postgres graph writer (nodes + edges)
│   │   └── worker.py           # queue consumer → chunk → embed → upsert
│   ├── retrieval/
│   │   ├── hybrid.py           # dense + BM25 + RRF + reranker
│   │   └── graph_query.py      # recursive CTEs for callers/callees
│   ├── agents/
│   │   ├── state.py            # AgentState TypedDict + reducers
│   │   ├── graph.py            # StateGraph assembly
│   │   ├── nodes/
│   │   │   ├── router.py       # Haiku classifier → route
│   │   │   ├── simple_rag.py   # single-shot: retrieve → answer
│   │   │   ├── agent_loop.py   # ReAct tool-use loop (max 8 iter)
│   │   │   ├── evaluator.py    # quality gate (groundedness/completeness)
│   │   │   ├── respond.py      # format answer + extract citations
│   │   │   └── clarify.py      # interrupt() for ambiguous queries
│   │   ├── subgraphs/
│   │   │   ├── code_executor.py   # write → sandbox → check → fix loop
│   │   │   └── deep_research.py   # planner → fan-out → rerank → synth
│   │   └── tools/
│   │       ├── search_code.py
│   │       ├── search_docs.py
│   │       ├── read_file.py
│   │       ├── grep.py
│   │       ├── web_search.py
│   │       ├── find_symbol.py
│   │       ├── find_callers.py
│   │       └── execute_code.py
│   ├── memory/
│   │   ├── checkpointer.py     # PostgresSaver factory
│   │   ├── store.py            # PostgresStore (long-term, per-user)
│   │   └── cache.py            # semantic query cache (Qdrant)
│   ├── api/
│   │   ├── app.py              # FastAPI
│   │   ├── routes/query.py     # POST /query → SSE stream
│   │   └── routes/webhooks.py  # POST /webhooks/{gitlab,confluence}
│   ├── mcp/
│   │   └── server.py           # MCP tools for Claude Code
│   └── eval/
│       ├── golden_set.jsonl    # 50 Q&A pairs with expected sources
│       └── run_eval.py         # recall@k, MRR, faithfulness
├── data/                       # gitignored
│   ├── repos/                  # git clone --mirror targets
│   └── confluence_cache/
└── tests/
```

---

## Phase 0 — Baseline RAG & Eval Harness

**Duration:** 3–5 days
**Framework:** None yet — pure scripts + LiteLLM + Qdrant
**Learn:** What embedding similarity actually retrieves. Why naive RAG fails. How to measure before optimizing.

### Deliverables

- [ ] `docker-compose.yml` (Qdrant + Postgres + LiteLLM)
- [ ] `litellm_config.yaml` with `agent-sonnet` + `embed` model aliases
- [ ] Script: clone 2 repos → naive 500-token fixed-window chunks → embed via LiteLLM → upsert to Qdrant
- [ ] Script: `query.py` — embed question → top-5 from Qdrant → stuff into Claude prompt → print answer
- [ ] `eval/golden_set.jsonl` — 30 real questions about your repos with known-correct file/line answers
- [ ] `eval/run.py` — computes recall@k, MRR, prints comparison table
- [ ] `make eval` works and shows baseline recall@10 (expect 0.4–0.6)

### Checkpoint

`make eval` runs end-to-end. You have a number. Every future phase must beat it.

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "Show me 3 queries that fail and explain *why* naive chunking broke them" | The failure modes that motivate Phase 1 |
| "Compare voyage-code-3 vs bge-m3 vs text-embedding-3-large on my eval set" | How to benchmark embedding models; what actually differs |
| "Help me write 10 golden-set questions for acme-auth" | What makes a good eval question; coverage strategy |
| "Walk me through what cosine similarity is actually matching here" | Deep intuition for embeddings — when they work and fail |
| "How does LiteLLM proxy the embedding call? Show me the wire format" | Understand the gateway you'll use everywhere |

---

## Phase 1 — Retrieval Quality: Chunking, Hybrid, Rerank

**Duration:** 1.5 weeks
**Learn:** AST-aware chunking. Dense vs sparse and when each wins. Reciprocal Rank Fusion. Cross-encoder reranking. Metadata filtering.

### Deliverables

- [x] `chunking/registry.py` — `ChunkerRegistry` keyed by file extension
- [x] `chunking/code.py` — tree-sitter AST chunker (Java, Python, Go, JS, TS; function/class boundaries, metadata: repo, path, language, symbol, start_line, end_line)
- [x] `chunking/markdown.py` — header-hierarchy splits with breadcrumb prefix
- [x] `chunking/fallback.py` — naive window for non-parseable files (YAML, JSON, SQL, etc.)
- [x] `chunking/service_metadata.py` — cross-service dependency extraction from `application.properties` + Java source
- [x] `retrieval/hybrid.py` — Qdrant dense + BM25 (sparse vectors), fused via RRF
- [x] `retrieval/reranker.py` — local `bge-reranker-v2-m3` cross-encoder, narrows 30 candidates → top-8
- [x] Context enrichment: `// Service: X | Layer: Y | Calls: Z | File: path | Func: name` prepended before embedding
- [x] Service-level metadata in payload: `service_name`, `depends_on`, `calls_services`, `layer` (filterable)
- [ ] `connectors/gitlab.py` — full-sync: clone repos, walk tree, chunk, embed, upsert (deferred to Phase 5)
- [ ] `connectors/confluence.py` — full-sync: paginate spaces via REST API, strip HTML → markdown (deferred to Phase 5)
- [ ] Re-run eval. Target: recall@10 > 0.85

### Service Metadata Enrichment (added)

Each chunk is enriched with cross-service context detected at index time:

```
Scanning application.properties → extracts:
  app.crypto.service-url → depends_on: ["crypto-service"]
  app.ratelimit.service-url → depends_on: ["ratelimit-service"]

Scanning Java source → extracts:
  @Value("${app.crypto.service-url}") → confirms dependency
  @PostMapping("/api/v1/enrollment") → exposed_endpoints

Per-file detection:
  filepath contains "controller" → layer: "controller"
  file references "crypto" → calls_services: ["crypto-service"]
```

Result: every chunk payload carries `{service_name, depends_on, calls_services, layer}`.
The text prefix bakes service relationships INTO the embedding so cross-service queries rank higher.

> **Known limitation:** Current `service_metadata.py` is Spring Boot–specific (assumes `application.properties` + `@*Mapping` annotations). Won't work for Go, Node, Python, or non-Spring Java repos. Pluggable extractors + `.ragmeta.yaml` manual override deferred to Phase 5.

### Checkpoint

Side-by-side eval report: Phase 0 vs Phase 1 scores per question. You can point at specific queries and explain what fixed them. Reranker should push the previously-failing "What endpoint handles device enrollment?" to ✓.

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "Show me tree-sitter chunk output for a real file — is function-level right or class-level?" | Granularity trade-offs; when to include surrounding context |
| "RRF vs weighted linear fusion vs learned fusion — trade-offs?" | Fusion strategies and when to tune weights |
| "My Confluence pages have giant tables — how do I chunk those?" | When NOT to embed; structured data strategies |
| "Reranker adds 200ms — is it worth it? Show me eval with/without" | Data-driven decision on latency vs quality |
| "LlamaIndex HierarchicalNodeParser vs hand-rolled — compare" | Framework ergonomics vs control; which to use where |
| "What's the right overlap for code vs docs chunks?" | None for code (AST boundaries); 10-15% for prose; why |
| "How does service_metadata help cross-service queries vs the code graph in Phase 2.5?" | Metadata enrichment (embedding-level) vs structural graph (query-level) — complementary |
| "Can I filter by `calls_services` in Qdrant? Show me the filter syntax" | Qdrant payload filtering + combining with vector search |

---

## Phase 2 — LangGraph Foundations: State, Router, Checkpointer

**Duration:** 1.5 weeks
**Learn:** `StateGraph`, `TypedDict` state with reducers, nodes, conditional edges, `PostgresSaver` checkpointer (= multi-turn memory), streaming, FastAPI SSE integration.

### Deliverables

- [ ] `agents/state.py`:
  ```python
  class AgentState(TypedDict):
      messages: Annotated[list, add_messages]
      query: str
      route: str  # "simple" | "agent" | "clarify"
      retrieved_chunks: Annotated[list, operator.add]
      reranked_chunks: list
      answer: str
      citations: list[dict]
      session_id: str
      iteration: int
  ```
- [ ] `agents/graph.py` — `START → router → {simple_rag | agent_loop} → evaluator → respond → END`
- [ ] `nodes/router.py` — Haiku call via LiteLLM classifies query complexity → sets `route`
- [ ] `nodes/simple_rag.py` — wraps Phase 1 retrieval, single LLM call, fast path
- [ ] `nodes/agent_loop.py` — Claude tool-use ReAct loop with tools: `search_code`, `search_docs`, `read_file`, `grep`, `web_search`
- [ ] `nodes/respond.py` — formats answer with inline `[SOURCE_N]` citations, extracts structured citation list
- [ ] `memory/checkpointer.py` — `PostgresSaver` factory (dev: local postgres, prod: RDS)
- [ ] `api/app.py` — FastAPI, `POST /query` with SSE streaming of graph events
- [ ] Phoenix integration: trace every graph invocation (see Phase 4.6)

### Checkpoint

1. Multi-turn works: ask "how does JWT validation work?" then follow up "what file is that in?" — second query resolves correctly using session history.
2. Router correctly sends simple lookups to `simple_rag` (fast, 1 LLM call) and complex questions to `agent_loop`.
3. View full graph trace in Phoenix: nodes visited, tools called, tokens used.

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "Draw the StateGraph. What goes in state vs recomputed each node?" | State design principles; avoiding bloat |
| "Router: Haiku classifier vs embedding-similarity vs rule-based — when does each win?" | Cost of routing; is an LLM call worth it |
| "Why PostgresSaver over RedisSaver or MemorySaver? Show me the checkpoint schema" | Durability vs speed; what's in the DB |
| "Hand-rolled tool loop vs LangGraph's `create_react_agent` — what do I lose/gain?" | Framework magic vs control |
| "How do I unit-test a LangGraph node in isolation?" | Testing strategy for graph-based systems |
| "Alternative: build this with raw Anthropic SDK + no LangGraph — what does LangGraph buy me?" | Honest framework ROI assessment |
| "Return deltas vs full state from nodes — show me what breaks with full state + reducers" | The bug the deep-dive doc got wrong |
| "OpenAI-format tools via LiteLLM vs native Anthropic tool_use — wire format difference?" | Understanding the translation layer |

### Citation System Detail

```python
# nodes/respond.py pattern
RESPONSE_PROMPT = """Answer using ONLY the provided context.
Rules:
1. Every claim MUST be followed by [SOURCE_N]
2. If chunks contradict, flag: "Conflict: ..."
3. If insufficient context, say so — do not hallucinate
4. Code examples must reference exact file:line from metadata

Context:
{context}

Question: {query}"""

# Source labels by type:
# code → [SOURCE_0] (code | acme-auth | src/auth/jwt.go:42)
# wiki → [SOURCE_1] (wiki | AUTH | OAuth > Refresh Tokens | https://...)
# web  → [SOURCE_2] (web | https://docs.example.com/...)
```

---

## Phase 2.5 — Code Graph: Structural Retrieval

**Duration:** 1 week
**Learn:** Why vector search can't answer "what calls X." Building a call/import graph from tree-sitter. Graph queries as agent tools.

### Deliverables

- [ ] `indexing/code_graph.py`:
  - Nodes: `(symbol_id, name, kind[func|class|method], file, repo, start_line)`
  - Edges: `(from_symbol, to_symbol, relation[calls|imports|defines|inherits])`
  - Built from tree-sitter parse: extract `import` statements + function call sites
  - Writes to Postgres tables with indexes on `(name, repo)` and `(relation, to_symbol)`
- [ ] `retrieval/graph_query.py` — recursive CTEs for:
  - `find_symbol(name)` → all definitions across repos
  - `find_callers(symbol_id, depth=1)` → who calls this
  - `find_callees(symbol_id, depth=1)` → what does this call
  - `trace_dependency(from_symbol, to_symbol)` → shortest path
- [ ] New agent tools registered: `find_symbol`, `find_callers`, `find_callees`, `trace_dependency`
- [ ] Add 10 graph-style questions to eval golden set
- [ ] Re-run eval with graph tools enabled

### Checkpoint

Agent correctly answers "what breaks if I change `ValidateToken`?" by chaining: `find_symbol("ValidateToken") → find_callers(result) → read_file(caller_locations) → synthesize impact`.

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "Postgres recursive CTE vs Neo4j vs NetworkX in-memory — at 50 repos, which?" | Right tool for the scale |
| "How do I handle cross-repo calls (service A imports B's client lib)?" | Monorepo vs polyrepo graph challenges |
| "Should the graph live in Qdrant payload or separate Postgres?" | Co-location vs separation; query patterns |
| "Sourcegraph SCIP/LSIF vs rolling my own — worth it?" | Build vs buy for code intelligence |
| "Where would LLM-extracted GraphRAG (Microsoft style) actually help me?" | When you have structure vs when you don't |
| "Show me the Postgres schema + a sample recursive CTE for 3-hop callers" | Concrete implementation reference |

---

## Phase 3 — Multi-Agent: CodeExecutor Subgraph + Sandboxing

**Duration:** 1.5 weeks
**Learn:** LangGraph subgraphs. Supervisor/worker pattern. Why isolation justifies a separate agent. E2B sandboxing. Prompt-injection defense.

### Deliverables

- [ ] `subgraphs/code_executor.py`:
  ```
  Subgraph: write_code → run_in_sandbox → check_output → {fix | done}
  Max 4 iterations. Own state: {code, stdout, stderr, exit_code, attempts}
  ```
- [ ] E2B integration:
  - Ephemeral sandbox per invocation
  - Network disabled
  - Target repo mounted read-only
  - 30s timeout, 512MB memory cap
- [ ] `tools/execute_code.py` — orchestrator tool that invokes the subgraph
- [ ] Output sanitization: sandbox stdout wrapped in `<sandbox_output>` delimiters; system prompt marks as untrusted data
- [ ] Prompt-injection defense: tool outputs never interpreted as instructions

### Checkpoint

Ask "does the regex in `validateEmail()` actually reject `foo@bar`?" → agent reads function → writes a test → runs in sandbox → reports result with evidence from actual execution.

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "Subgraph vs tool-that-calls-another-graph vs separate process — actual LangGraph difference?" | When subgraphs justify their complexity |
| "E2B vs Docker+gVisor vs Modal — security/cost/latency for my threat model" | Sandbox comparison for real workloads |
| "How do I pass parent context into subgraph without blowing token budget?" | State handoff patterns |
| "Show me 3 prompt-injection attacks via sandbox output and how delimiter defense stops them" | Practical security for agentic systems |
| "When should orchestrator route to CodeExecutor vs just answering from retrieved code?" | Router logic for execution vs explanation |
| "Can I run the sandbox locally in dev without E2B?" | Docker `--network=none --read-only` as fallback |

---

## Phase 3.5 — Autonomous Local Ingestion + Web UI

> Pulled forward from Phase 5. Drop a repo into `data/repos/` or a PDF/MD into `data/docs/` → auto-indexed without running a command. Plus a chat UI so you can stop using `make ask`.

**Duration:** 1–2 sessions
**Learn:** Qdrant native BM25 (server-side tokenization + IDF). Idempotent per-source upserts. Filesystem events (watchdog) + debouncing. Producer/consumer queues. PDF text extraction (PyMuPDF). FastAPI lifespan + background threads. SSE consumption from a browser.

### Why now (out of original order)

- Hand-rolled `vocab.json` breaks the moment indexing goes incremental — switching to native BM25 is the prerequisite, and it's also a quality upgrade (proper TF-IDF vs our TF-only).
- The CLI (`make ask`) is fine for testing but a chat UI makes the system *usable* day-to-day, which surfaces real failure modes faster than scripted tests.

### Deliverables

**A. Qdrant native BM25 (foundation)**
- [ ] `retrieval/hybrid.py` — drop vocab; use `models.Document(text=...)` for sparse, `SparseVectorParams(modifier=Modifier.IDF)` on collection
- [ ] `scripts/index_repos_v1.py` — remove `build_vocab_from_chunks` / `compute_sparse_vector`; let Qdrant tokenize server-side
- [ ] Delete `data/vocab.json`; one-time `make index-v1` to migrate. No Qdrant redeploy needed (≥1.10 already supports it)

**B. Docs pipeline + `search_docs`**
- [ ] `ingest/pdf.py` — PyMuPDF: per-page text, font-size → heading inference → markdown; carries `page_number`
- [ ] New Qdrant collection `docs` (same dense+sparse config); chunks via existing `chunk_markdown_file()`
- [ ] `agents/tools/search_docs.py` — hybrid+rerank over `docs` collection; registered in `ALL_TOOLS`
- [ ] Citations for docs: `[SOURCE_N]: docs/file.pdf p.7`

**C. Incremental indexer (repo-level AND file-level)**
- [ ] Stable Qdrant point IDs: `uuid5(NAMESPACE, f"{repo}|{filepath}|{chunk_idx}")` so re-upsert replaces in place
- [ ] `ingest/incremental.py`:
  - `index_repo(name, files: list[str] | None = None, deleted: set[str] = ∅)`:
    - `files=None` → **full rebuild**: delete all chunks/symbols for repo → walk everything → upsert + extract graph
    - `files=[...]` → **file-level**: for each path: Qdrant delete-by-filter `(repo, filepath)` + Postgres `DELETE code_symbols WHERE repo AND filepath` (CASCADE outgoing, SET NULL inbound) → re-chunk/embed/extract just that file
    - After either path: `resolve_edges()` re-links orphaned `to_symbol IS NULL` globally → cross-repo edges self-heal
  - Escalation rules (force `files=None`):
    - Any changed file matches `service_metadata.metadata_source_patterns(repo_path)` — a per-repo, auto-detected list (Spring → `application.properties`/`*.gradle`; Go → `go.mod`; Node → `package.json`; Rust → `Cargo.toml`; Python → `pyproject.toml`; Helm → `values.yaml`; always includes `.ragmeta.yaml`). Repos with no detected build system return only `[".ragmeta.yaml"]` → file-level always applies. Forward-compatible with Phase 5's pluggable extractors (each contributes its patterns)
    - OR `len(files) > 0.2 * repo_file_count` (mass change → full rebuild is faster)
  - `index_doc(path)` / `delete_doc(path)` — same pattern on `docs` collection
  - `delete_repo(name)` — full removal
- [ ] Refactor `index_repos_v1.py` → thin wrapper looping `index_repo(name, files=None)` over all repos
- [ ] `make index-add REPO=X [FILES="a.java b.java"]`, `make index-doc FILE=path`

> **Cross-repo graph consistency:** handled by FK design — `from_symbol ON DELETE CASCADE` (outgoing edges die with the file), `to_symbol ON DELETE SET NULL` (inbound edges orphan), then global `resolve_edges()` re-links by name. New repo's edges resolve against existing repos' symbols and vice versa. No manual graph maintenance needed.

**D. Filesystem watcher**
- [ ] `ingest/watcher.py` — `watchdog.Observer` with two handlers:
  - `ReposHandler` on `data/repos/**` → collects per-repo `{changed_files, deleted_files}` during 3s debounce window → emits `IndexRepoJob(name, files, deleted)`; if repo dir is new/removed → `files=None` (full)
  - `DocsHandler` on `data/docs/*.{pdf,md,txt}` → debounce per-file (1s) → `IndexDocJob`
  - Worker thread consumes `queue.Queue`, calls incremental indexer, logs progress
  - Handles create/modify/delete; ignores `.git`, `.DS_Store`, temp files
- [ ] `scripts/watch.py` — standalone entry (`make watch`)
- [ ] `api/app.py` — FastAPI lifespan: spawn watcher thread on startup (gated by `WATCHER_ENABLED=true`)

**E. Web UI portal (Gradio)**
- [ ] `api/ui.py` — `gr.Blocks` with `gr.Chatbot` + `gr.Textbox`:
  - Generator function streams graph events → yields incremental messages (router decision, each tool call, final answer with citations) into the chat history
  - Session dropdown (lists checkpointer `thread_id`s) + "New session" button
  - Sidebar accordion: indexed repos (from Qdrant payload distinct) + docs (from `docs` collection)
  - Citations rendered as markdown links under each answer
- [ ] `api/app.py` — `gr.mount_gradio_app(api, ui, path="/ui")`; FastAPI lifespan still owns watcher thread
- [ ] `GET /sources` — list indexed repos + docs; `GET /sessions` — list thread_ids (Gradio calls these to populate sidebar/dropdown)
- [ ] `make serve` → `http://localhost:8000/ui` is the chat; `:8000/query` and `:8000/docs` (OpenAPI) still work

### Checkpoint

1. `cp -r ~/some-repo data/repos/new-repo` → within ~5s watcher logs "indexed new-repo: N chunks, M symbols" → `make ask Q="..." S=t` finds content from new-repo.
2. `cp design.pdf data/docs/` → `search_docs("...")` returns chunks with `page_number`.
3. `rm -rf data/repos/new-repo` → watcher removes its chunks/symbols.
4. Open `http://localhost:8000/` → ask a question → see route, tool calls stream in, answer renders with clickable citations.

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "Why does copying a repo fire 1000 events? Show me the debounce timer pattern" | FSEvents granularity; per-key coalescing |
| "Qdrant `Modifier.IDF` — what's it computing server-side that we weren't?" | TF vs TF-IDF vs BM25 saturation; why IDF needs the whole corpus |
| "Delete-by-filter then insert vs true upsert by stable ID — which and why?" | Idempotency strategies; what happens on partial failure |
| "PyMuPDF: how do I infer headings from font size? Show me a real PDF's blocks" | PDF text extraction internals |
| "Watcher in a thread vs asyncio task vs separate process — trade-offs in FastAPI?" | Concurrency models; GIL impact on the indexer |
| "How does Gradio's generator streaming map to LangGraph's `stream()`?" | Yielding partial chat history; why each yield replaces (not appends) |
| "`gr.mount_gradio_app` vs running Gradio standalone — what's shared?" | Single ASGI app; FastAPI lifespan owns background threads |
| "When would I outgrow Gradio for React/Next?" | Custom interactions (inline code editing, graph viz, fine-grained tool-call UI) |

---

## Phase 4 — Memory, Parallelism, Human-in-the-Loop

**Duration:** 1.5 weeks
**Learn:** Long-term memory (Store). Semantic caching. `Send` API for map-reduce fan-out. `interrupt()` for clarification. State reducers for parallel merge. History summarization.

### Deliverables

- [ ] `memory/store.py` — `PostgresStore` namespaced by `user_id`; orchestrator reads relevant memories at graph entry, writes observations at exit
- [ ] `memory/cache.py` — embed query → Qdrant `query_cache` collection → if sim > 0.95, return cached answer (skip graph entirely)
- [ ] `subgraphs/deep_research.py`:
  ```
  Planner → decompose into N sub-questions (each tagged code|wiki|docs|web)
  → Send() fans out parallel retrieval per sub-question
  → Merge via Annotated[list, operator.add] reducer
  → Rerank all results against original query
  → Synthesize with citations
  ```
- [ ] `nodes/clarify.py` — `interrupt()` when router confidence < threshold; surfaces as question in Slack/API
- [ ] `nodes/summarize_history.py` — fires when `len(state.messages) > 20`, compresses old turns into summary
- [ ] State schema uses `Annotated[list, operator.add]` for `retrieved_chunks` to handle parallel writes correctly

### Checkpoint

1. Ask same question twice — second is instant (semantic cache hit, visible in trace).
2. Ask "audit all repos for hardcoded secrets" — see parallel fan-out in Phoenix trace (multiple retrieval calls concurrent).
3. Ask something ambiguous — get a clarifying question back; answer it; conversation resumes.
4. Have a 25-message conversation — history gets summarized without losing context.

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "What belongs in long-term Store vs checkpointer vs semantic cache? Decision tree?" | Three-layer memory architecture |
| "Send API vs asyncio.gather inside a node — why use the LangGraph primitive?" | Framework-managed parallelism vs DIY |
| "How do I prevent Store from accumulating garbage? Eviction strategy?" | Memory lifecycle management |
| "interrupt() in SSE/REST vs Slack — how does the resume flow differ?" | Async human-in-the-loop patterns |
| "Reducers: operator.add vs custom dedup-merge vs add_messages — what does each do?" | Parallel state merging deep-dive |
| "Mem0 or Zep for long-term memory instead of LangGraph Store — compare" | Framework-native vs specialized memory |
| "Show me the deep_research fan-out for a real query — trace the state through each step" | Concrete understanding of planner→fan-out pattern |

---

## Phase 4.5 — Evaluator / Self-Correction Loop

**Duration:** 3–4 days
**Learn:** LLM-as-judge. Quality gates in agent loops. When to retry vs accept.

### Deliverables

- [ ] `nodes/evaluator.py`:
  - Haiku via LiteLLM scores answer on groundedness (0–1) + completeness (0–1)
  - If combined score < 0.6 AND iteration < 2 → route back to agent_loop with refined query
  - If score >= 0.6 or max retries → proceed to respond
- [ ] Feature flag: evaluator on/off (skip for `simple_rag` route — not worth the latency there)
- [ ] Log evaluator scores to Phoenix for quality monitoring over time
- [ ] Add 10 "adversarial" eval questions (ambiguous, multi-part, contradicting sources)

### Checkpoint

Ask a question where initial retrieval misses the mark — watch evaluator trigger a retry with refined sub-questions, second attempt succeeds.

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "LLM-as-judge vs heuristic (citation-count, retrieval-score threshold) — when is the LLM call worth it?" | Cost of quality vs proxy metrics |
| "Should the evaluator see the sources or just the answer? Trade-offs" | Groundedness checking strategies |
| "How do I tune the 0.6 threshold? What data do I need?" | Calibration methodology |
| "Can the evaluator be the same model that generated the answer? Bias?" | Self-eval limitations |

---

## Phase 4.6 — Observability: Phoenix Tracing

> Pulled forward from Phase 6. Debugging "why did the router pick X / why didn't clarify fire / what did query_analysis return" by reading `print()` lines in the server log doesn't scale past ~10 nodes. We need per-request, per-node, per-LLM-call traces *now*.
>
> Originally planned with Langfuse; switched to Arize Phoenix because Langfuse v3 self-host requires ClickHouse + Redis + MinIO (4 containers) and the v2 SDK is incompatible with langchain ≥1.0. Phoenix is one container, OTel-native, auto-instruments LangChain.

**Duration:** 1 session
**Learn:** OpenTelemetry spans. OpenInference semantic conventions. Auto-instrumentation vs callback-based. Span hierarchy (trace → node span → LLM/tool span). Pushing custom scores as span events. Linking UI answers to traces.

### Deliverables

- [ ] `docker-compose.yml` — add `arizephoenix/phoenix` service (single container, SQLite-backed, persists to `data/phoenix/`)
- [ ] `pyproject.toml` — add `arize-phoenix-otel` + `openinference-instrumentation-langchain`
- [ ] `src/rag/config.py` — `PHOENIX_HOST`, `PHOENIX_PROJECT`, `PHOENIX_ENABLED`
- [ ] `src/rag/tracing.py`:
  - `init_tracing()` — called ONCE at process start; `phoenix.otel.register()` + `LangChainInstrumentor().instrument()`. Idempotent, no-op when disabled or packages missing.
  - `trace_config(thread_id, user_id, query, tags) -> (config, Tracer)` — builds the RunnableConfig and a context-manager that attaches `session.id` / `user.id` / metadata via OpenInference `using_session` / `using_user`.
  - `Tracer.url()` / `.score(name, value)` / `.flush()`; `push_scores()` attaches eval+guard outputs as span events.
- [ ] Wire into all three entry points: `api/app.py` (lifespan + `with tracer:` around invoke), `api/ui.py`, `scripts/ask.py`
- [ ] UI: append `[🔍 trace](http://localhost:6006/...)` link under each answer

### Checkpoint

1. `docker compose up -d phoenix` → http://localhost:6006 shows the project.
2. Ask any question in the UI → click the 🔍 link → see the full node tree with `query_analysis` LLM input/output expanded.
3. Filter traces by `session.id == <thread_id>` to see all turns of one conversation.
4. With `PHOENIX_ENABLED=false`, everything still works (init is a no-op).

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "How does `LangChainInstrumentor` hook in without me passing a callback?" | Monkey-patching `BaseCallbackManager`; OTel context propagation |
| "OpenInference vs raw OTel semantic conventions — what's `llm.input_messages`?" | The schema Phoenix uses to render prompts/completions |
| "Phoenix vs Langfuse vs LangSmith — when does each win?" | Single-container vs feature-rich vs hosted; OTel portability |
| "How do I attach the cache hit/miss decision to the trace if it happens BEFORE the graph runs?" | Manual span via `tracer.start_as_current_span("cache_lookup")` |
| "Sampling: do I trace 100% in dev, what % in prod?" | OTel `TraceIdRatioBased` sampler; head vs tail sampling |
| "Phoenix evals (`phoenix.evals`) vs my evaluator.py — overlap?" | Built-in LLM-judge vs custom; running evals over stored traces |
| "Can I export Phoenix spans to Grafana/Tempo later?" | OTel collector fan-out; vendor portability |

---

## Phase 5 — Multi-Source Knowledge + Tiered Retrieval

> **AS-BUILT differs from the original plan below.** What shipped (tag `phase-5`, see `PHASE5_ARCHITECTURE.md`):
> - ✅ **Confluence connector** (Data Center PAT, not GitLab) — 24h version-diff sync, storage-format→markdown, `delete_space` reset, scheduler thread, URLs as citation links
> - ✅ **Git-history tools** (on-demand, no index) — `git_log`, `git_show`, `git_blame`, `find_commits_for_jira` (cross-repo)
> - ✅ **JIRA live lookup** (on-demand) — `jira_lookup`, `jira_search`; completes the ticket→commits→wiki→code join
> - ✅ **Retrieval cascade** — code → local-docs → Confluence tiers with early-exit (NOT in original plan; the standout addition)
> - ✅ **Guard/routing fixes** — citation-aware abstain, relatedness bands (general-knowledge answers survive), `_DOCS_RE` routing, clarify-loop fixes
> - ✅ **Repo cleanup** — tests→`tests/`, full-index→`rag.ingest.full_index`, Phase-0→`scripts/legacy/`
> - ⏭ **Deferred:** GitLab/Confluence *webhooks* + queue workers (24h poll suffices), **MCP server**, **Slack** — moved toward Phase 6 / on-demand
> - ⏭ **Still pending:** pluggable `service_metadata/` (Spring-only today)
>
> The original webhook/MCP/Slack plan is preserved below for reference.

**Duration:** 1.5 weeks
**Learn:** Webhook-driven incremental indexing. Idempotent upserts. Queue decoupling. MCP protocol. Slack Bolt.

### Deliverables (original plan — partially superseded by as-built above)

- [ ] `connectors/gitlab.py` — full-sync: clone/pull repos via GitLab API, walk tree, chunk, embed, upsert
- [ ] `connectors/confluence.py` — paginate Confluence spaces via REST API, strip storage-format HTML → markdown, chunk
- [ ] Refactor `service_metadata.py` → pluggable `service_metadata/` package:
  - `detectors.py` — detect repo type (Spring, Go, Node, Python) from build files
  - `spring.py`, `go.py`, `node.py`, `python.py`, `helm.py` — per-stack extractors
  - `manual.py` — `.ragmeta.yaml` override at repo root (source of truth when auto-detect fails)
  - Orchestrator merges results from all applicable extractors
- [ ] `scripts/index_add.py` — incremental single-repo indexer (`make index-add REPO=X`):
  - Does NOT delete entire collection
  - Deletes only points where `payload.repo == REPO` (removes stale chunks for that repo)
  - Chunks + embeds + upserts the repo's new chunks
  - Appends new terms to vocabulary
  - Enables adding repos without re-indexing everything (~2 min → ~15 sec per repo)
- [ ] `api/routes/webhooks.py`:
  - `POST /webhooks/gitlab` — validate `X-Gitlab-Token`, extract push event → enqueue changed file paths
  - `POST /webhooks/confluence` — validate shared secret, extract page-updated event → enqueue page ID
- [ ] `indexing/worker.py`:
  - Consumes Redis Streams (dev) or SQS (prod) via `Queue` protocol
  - For git: fetch diff via GitLab API → re-chunk only changed files → upsert keyed `(repo, path, chunk_idx)` → delete orphaned chunks for deleted files
  - For Confluence: fetch page content → re-chunk → upsert keyed `(space, page_id, chunk_idx)`
  - Idempotent: same webhook replayed = same result
- [ ] `mcp/server.py` — exposes tools as MCP (stdio + SSE transports):
  - `search_code`, `search_docs`, `read_file`, `find_callers` (Claude Code becomes the agent)
- [ ] Slack Bolt app (socket-mode for dev):
  - Receives message → calls `/query` endpoint → streams response → renders citations as links
- [ ] Dev workflow: ngrok tunnels GitLab → localhost; or `make reindex REPO=X` for manual trigger

### Checkpoint

Push a commit to a test repo → within 30s, query about the new code returns correct answer. Claude Code (via MCP) calls `search_code` and gets results.

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "Webhook payload has commit SHAs — how do I efficiently get file diffs without re-cloning?" | GitLab API: compare commits, list changed files |
| "Idempotency: what's my upsert key? What happens on replay/duplicate?" | Exactly-once semantics in practice |
| "MCP stdio vs SSE transport — which for Claude Code, which for other clients?" | MCP transport tradeoffs |
| "Should MCP expose raw tools or the full orchestrator graph?" | Agent-as-client vs agent-as-proxy |
| "Poll GitLab every 60s instead of webhooks — when is that actually fine?" | Simplicity vs freshness tradeoffs |
| "How do I handle Confluence storage-format XML → clean markdown?" | Practical HTML/XML stripping |

---

## Phase 6 — Production Deployment

> **Status — DONE (`phase-6`), diverged from plan ↓.** Shipped a
> single-host **Docker Compose on EC2** stack instead of EKS:
> `Dockerfile` + `app` service alongside qdrant/postgres/phoenix,
> bind-mounted `./data` + `./hf-cache` (reranker pre-warmed, not
> baked). Added the **`cto` terminal client** (rich/prompt_toolkit;
> Claude-Code-style ⏺/⎿; self-animating spinner; slash commands) with
> a `--remote` thin-SSE mode that needs only httpx+rich on the laptop.
> Security landed as **layer-1**: bearer keys (`CTO_API_KEYS`,
> sha256+constant-time compare) on `/query`/`/sources`/`/sessions`,
> Gradio login (`CTO_UI_USER/PASSWORD`), and an oauth2-proxy compose
> profile (`--profile oidc`) that injects `X-Forwarded-User` →
> trusted `user_id`. Runbook in `docs/DEPLOY.md`; diagrams in
> `docs/PHASE6_ARCHITECTURE.md`.
>
> **Deferred to Phase 7:** EKS/Terraform/Helm, KEDA, per-user repo
> ACLs in retrieval, rate limiting, Go remote-CLI binary,
> `PHOENIX_PUBLIC_HOST` rewrite, `CTO_REQUIRE_AUTH` fail-closed.

**Duration:** 2 weeks
**Learn:** Dev↔prod config parity. EKS deployment. KEDA autoscaling. Secrets management. Security hardening. Cost monitoring.

### Deliverables

- [ ] `config.py` — `ENVIRONMENT={dev,prod}` controls all backends:
  ```python
  # Same code, different infra:
  # dev:  Qdrant localhost:6333, Postgres localhost:5432, Redis localhost:6379, LiteLLM localhost:4000
  # prod: Qdrant Cloud, RDS Multi-AZ, SQS, LiteLLM on EKS (internal service)
  ```
- [ ] Terraform/Pulumi: VPC, EKS, RDS, SQS, Qdrant Cloud, ALB, Secrets Manager, ECR
- [ ] Helm chart / Kustomize: 3 deployments:
  - `api` (2–10 replicas, HPA on p95 latency + RPS)
  - `indexer-worker` (1–20 replicas, KEDA on SQS queue depth)
  - `mcp-server` (2 fixed replicas)
  - `litellm` (2–4 replicas, internal Service)
- [ ] LiteLLM prod config: Bedrock model endpoints, virtual keys per team, budgets
- [ ] GitLab CI: build → ECR → ArgoCD sync
- [ ] Observability: Langfuse + Prometheus/Grafana dashboards (latency, cost/query, recall, indexer lag)
- [ ] Security hardening:
  - Webhook secret validation (reject unsigned)
  - Prompt-injection delimiters on all tool outputs
  - Secret-file skip-list in indexer (`.env*`, `*.pem`, `*credentials*`) + trufflehog scan pre-embed
  - Rate limiting per user (Redis/ElastiCache token bucket)
  - Sandbox network isolation (E2B or gVisor)
  - Audit log → S3 (every query + chunks + answer)

### Checkpoint

`kubectl get pods` healthy. Load test 50 concurrent queries. Push to repo → indexer-worker scales via KEDA. Cost dashboard shows $/query. All secrets in Secrets Manager, none in env.

### Prod Architecture

```
┌──────────────────────────────── VPC ────────────────────────────────┐
│                                                                      │
│  GitLab ──webhook──▶ API GW → SQS ◀── Confluence webhook            │
│                          │                                           │
│                          ▼                                           │
│  ┌─────────────────────────┐     ┌──────────────────────────────┐   │
│  │ indexer-worker (KEDA)   │────▶│ Qdrant Cloud (vectors+BM25)  │   │
│  │  chunk + embed + upsert │────▶│ RDS Postgres (graph+ckpt+mem)│   │
│  └─────────────────────────┘     └────────────────▲─────────────┘   │
│                                                   │                 │
│  ALB ──▶ ┌──────────────────┐                     │                 │
│          │ api (HPA)        │─────────────────────┤                 │
│          │  LangGraph       │──▶ LiteLLM svc ──▶ Bedrock            │
│          │  orchestrator    │──▶ E2B (sandbox)                      │
│          └──────────────────┘──▶ Tavily (web)                       │
│                                                                      │
│  NLB ──▶ mcp-server ──▶ (same Qdrant/RDS)                          │
│                                                                      │
│  Langfuse ◀── traces     Prometheus/Grafana ◀── metrics             │
└──────────────────────────────────────────────────────────────────────┘
```

### Cost Estimate (~500 queries/day, 50 repos)

| Item | ~Monthly |
|---|---|
| Qdrant Cloud | $300 |
| RDS Postgres Multi-AZ | $350 |
| EKS + nodes | $400 |
| Bedrock Claude (via LiteLLM) | $150–400 |
| Voyage embeddings | $30 |
| Cohere rerank | $50 |
| E2B | $100 |
| Tavily | $50 |
| Langfuse | $100 |
| ALB/NAT/SQS/misc | $150 |
| **Total** | **~$1.7–2k/mo** |

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "Terraform module structure — one stack or per-component?" | IaC organization for this system |
| "Bedrock vs Anthropic API direct — latency, cost, model version lag" | Provider tradeoff in prod |
| "Qdrant Cloud vs self-hosted on EKS — at what scale does self-host win?" | Managed vs ops cost |
| "Show me the prompt-injection threat model for this system specifically" | Concrete attack surface |
| "How do I run eval in CI as a regression gate?" | Quality CI/CD |
| "ECS Fargate instead of EKS — what do I give up?" | Simpler compute at the cost of flexibility |
| "LiteLLM router strategies (shuffle vs least-busy vs latency-based) — which for my traffic?" | Gateway optimization |

---

## Phase 7 — Skills: pluggable multi-step playbooks

> **Status — DONE (`phase-7`).** All 8 steps shipped (7-A…7-H +
> 7-F+ live streaming). Two-mode `DockerSandbox`; `cto-secaudit`
> image; `make_run_shell_tool`/`write_report`/`ask_user`; registry
> (`data/skills/*.md`, fail-soft); `skill_runner` =
> `create_react_agent(prompt=preamble+body, checkpointer)` with
> container lifecycle + `get_stream_writer()` custom events for
> live ⏺/⎿; router explicit/trigger/slash + `GET /skills`;
> `onboarding-tour` (no-sandbox) + `security-audit`
> (persistent). Diagrams: `docs/PHASE7_ARCHITECTURE.md`.
>
> **Diverged from plan:** used the prebuilt agent (`prompt=`
> kwarg), not hand-rolled — verified `interrupt()` works directly
> inside a tool with `PostgresSaver`, so the 7-D sentinel pattern
> was dropped. No tool-alias layer — tools are named to match
> host-agent skill conventions.
>
> **Known limits / follow-ups:** inner-interrupt bridge handles
> ONE `ask_user` per run; `cto-secaudit` not yet live-built;
> skill tool-calls in the Gradio trace block untested in
> browser; `agent_loop_prebuilt` still uses removed
> `state_modifier=` (unused).

**Goal:** make CTO run a *skill* — a markdown file describing a
multi-phase procedure (the motivating example: a security-audit skill
that runs `semgrep`/`tfsec`/`gitleaks`/`trivy` against a repo and
writes a scored report). This is the jump from "agent that answers
questions" to "agent that executes procedures".

**Learn:** the difference between a *tool* (one capability, one call)
and a *skill* (a system prompt + a curated toolset + sandbox config
that turns the agent into a specialist for the duration of one task).
How Claude Code / Devin / OpenAI Assistants model "instructions as
data". How to give an agent a longer leash (higher `max_iter`,
write-file capability) safely by narrowing the toolset and the
filesystem surface at the same time.

### What is a skill, mechanically?

- A **markdown file** with YAML frontmatter (`name`, `description`,
  `triggers`, `sandbox: {mode: ephemeral|persistent, image, timeout,
  run_timeout, network, memory}`, `max_iter`, `tools_allowed`) and a
  **body** that becomes the system prompt.
- It does NOT add new code. It *configures* an agent_loop variant:
  which tools it may call, which sandbox image those tools run in,
  how many ReAct iterations it gets, and what the LLM is told to do.
- Loaded at startup from `data/skills/*.md`; hot-reloadable like docs.

### Skill catalogue — what the engine should be able to run

Security-audit is the *driver*, not the architecture. The same
registry + `skill_runner` should run all of these unchanged; the
spread of `mode`/`network`/toolset is what proves generality. For
later discussion: which 2–3 to ship after the audit skill works.

| Skill | `mode` | `image` | `net` | `tools_allowed` (key ones) | What it does / why this config |
|---|---|---|---|---|---|
| `security-audit` | persistent | `cto-secaudit` | ✅ | run_shell, read_file, grep, write_report, ask_user, git_log | Discover stack → runtime-install auditors → multi-phase scan → scored report. Exercises *everything*. |
| `dependency-audit` | persistent | `cto-secaudit` | ✅ | run_shell, read_file, write_report | Just Phases 1–2 of the above (CVE table). Same image, narrower playbook. |
| `terraform-plan-review` | persistent | `hashicorp/terraform` | ✅ | run_shell, read_file, write_report, ask_user | `init` → `plan -out` → LLM explains drift/risk. Off-the-shelf image, no custom build. |
| `test-coverage` | persistent | `cto-secaudit` (toolchains) | ✅ | run_shell, write_report | `npm test --coverage` / `pytest --cov` / `go test -cover` → summarize gaps. |
| `perf-profile` | persistent | `cto-perf` (py-spy, async-profiler, hyperfine) | ❌ | run_shell, read_file, write_report | Run benchmarks, profile, report hot paths. Network off — nothing to fetch. |
| `license-audit` | ephemeral | `cto-secaudit` | ❌ | run_shell, read_file, write_report | `scancode`/`licensee` one-shot. Pure-function scan, no chain. |
| `api-contract-diff` | *(none)* | — | — | read_file, grep, search_code, git_log, git_show, write_report | "What API changes between v1.2 and v1.3?" — **no `run_shell`**, pure read tools. Proves a skill needn't touch the sandbox at all. |
| `onboarding-tour` | *(none)* | — | — | search_code, read_file, repo_info, find_callers, find_callees | "Walk me through this repo for a new hire" — long structured prompt over existing RAG tools. |
| `incident-postmortem` | *(none)* | — | — | search_code, git_log, git_show, find_commits_for_jira, jira_lookup, read_file, write_report | Given a JIRA incident ID: pull ticket → find related commits → reconstruct timeline → write postmortem. Shows skills composing **git+JIRA tools**, no shell. |
| `release-notes` | *(none)* | — | — | git_log, git_show, find_commits_for_jira, jira_lookup, write_report | `git log v1.2..v1.3` → group by JIRA → human-readable changelog. |

**Build-order implication:** `api-contract-diff`, `onboarding-tour`,
`incident-postmortem`, `release-notes` need **only 7-C/D/E/F/G** —
no sandbox work. They can ship before the secaudit image exists,
and they're the cheapest way to validate the registry +
`skill_runner` end-to-end.

### Deconstruction — build these in order, each is independently testable

- **7-A · Sandbox knobs + two execution modes** (foundation —
  touch nothing else first)
  - **Ephemeral mode** (today's `--rm`-per-call, tightened):
    `DockerSandbox.run(cmd, *, image, timeout, network, read_only)`
    — thread the kwargs through `_build_cmd()`. Default stays
    `network=False, read_only=True`.
  - **Persistent mode** (new — one container per skill *run*):
    ```
    cid = DockerSandbox.start(image, repo, *, network, memory,
                              run_timeout)        # docker run -d … sleep infinity
    DockerSandbox.exec(cid, cmd, *, timeout)     # docker exec cid sh -lc '…'
    DockerSandbox.stop(cid)                       # docker rm -f cid
    DockerSandbox.reap(prefix, older_than)        # janitor for abandoned runs
    ```
    State (env, cwd, installed packages, `/work` files) persists
    across `exec` calls; the container is still `--cap-drop ALL`,
    `/repo:ro`, `--pids-limit`, host-isolated. The trade vs
    ephemeral: rootfs is writable (so `pip install` works,
    container-local) and `network` may be on (so PyPI is reachable).
  - **Why two modes:** the security-audit skill *discovers* the
    tech stack (Phase 1) then runtime-installs stack-specific
    auditors (`npm audit`, `pip-audit`, `cargo audit`, `gosec`) and
    runs build-dependent chains (`npm install`→`npm audit`,
    `terraform init`→inspect). That requires state across calls.
    Simpler skills (one-shot static scanners only) keep ephemeral.
  - **Test:** ephemeral — `run("sleep 35", timeout=60)` succeeds,
    `run("curl x", network=False)` fails. Persistent —
    `start()`→`exec(cid,"echo a > /work/x")`→`exec(cid,"cat /work/x")`
    returns `"a"`; `exec(cid,"touch /repo/x")` fails (`:ro`);
    `stop(cid)` removes it; `reap("skill-", 0)` cleans orphans.
  - **Learn:** which Docker isolation flags are *security-essential*
    (host fs, caps, `/repo:ro`) vs. *convenience defaults*
    (`--read-only`, `--network=none`) you can relax per-skill
    without giving the LLM your machine.

- **7-B · Scanner image** (`cto-secaudit:latest`)
  - `src/rag/sandbox/Dockerfile.secaudit`. Two layers of tooling:
    - **Cross-stack scanners (always pre-baked):** `semgrep`,
      `trivy`, `gitleaks`, `tfsec`, `checkov`, `bandit`. These
      cover Phases 3–7 regardless of language and work offline.
    - **Language toolchains (pre-baked, small):** `node`+`npm`,
      `python`+`pip`, `go`, `cargo`, `maven`/`gradle` wrappers.
      Their *project* installs (`npm install`, `pip install -r`)
      and stack-specific auditors the LLM picks at runtime
      (`gosec`, `brakeman`, `bundler-audit`, …) happen IN the
      persistent container during the run.
  - `make sandbox-secaudit` target. Bake offline rule bundles for
    the cross-stack scanners so a `network=false` skill still works.
  - **Test:** `docker run --rm cto-secaudit semgrep --version`;
    `… npm --version`; `… go version`.
  - **Learn:** the split between *capability* you bake (toolchains
    + universal scanners) and *project state* the run discovers
    (deps, niche auditors). Why baking *every* possible scanner
    (5 GB image, stale within a month) is the wrong axis.

- **7-C · Two new tools** (the missing primitives)
  - `run_shell(cmd)` — built **per skill** by `make_run_shell_tool(
    sandbox_cfg, repo, cid=None)`. The LLM only passes `cmd`;
    image/timeout/network/cid are closed over so the model can't
    override sandbox config. Dispatches by mode:
    `ephemeral` → `DockerSandbox.run(cmd, image=…, timeout=…)`;
    `persistent` → `DockerSandbox.exec(cid, cmd, timeout=…)`.
    Returns `{exit_code, stdout[:50KB], stderr, duration_ms}`.
  - `write_report(repo, filename, content)` — writes under
    `data/reports/<repo>/<filename>` ONLY (path-jail: reject `..`,
    absolute paths, anything outside that root). Size cap 2 MB.
    Optionally `index_doc()` the result so the report becomes
    searchable.
  - **Test:** `run_shell("ls /repo", repo="acme-api")` lists
    files; `write_report("x", "../../../etc/passwd", "…")` is
    rejected.
  - **Learn:** the "tool boundary" — a tool is the ONLY way the LLM
    can affect the world. Path-jail and stdout caps are the security
    model; the LLM can ask for anything, the tool decides what's
    allowed.

- **7-D · `ask_user` via `interrupt()`** — *verified, simpler than
  first thought*
  - The skill's `ask_user` ("fix CRITICAL only?") maps to LangGraph
    `interrupt()` — same mechanism as `clarify`.
  - **A tool CAN call `interrupt()` directly** (smoke-tested with
    `create_react_agent` + `PostgresSaver`): the prebuilt's
    `ToolNode` is a graph node, so the checkpointer/resume mechanism
    applies inside the tool's call stack. No sentinel pattern needed.
    ```python
    @tool
    def ask_user(question: str, options: list[str] | None = None) -> str:
        return str(interrupt({"kind": "skill", "question": question,
                                "options": options or []}))
    ```
    `interrupt()` checkpoints + raises → CLI/UI shows the ❓ panel →
    `Command(resume=ans)` → `interrupt()` *returns* `ans` → that
    string IS the ToolMessage content → loop continues.
  - **Learn:** human-in-the-loop inside a ReAct loop. Why this
    differs from `clarify` (which runs *before* routing): here the
    pause can happen at iteration 17 of 60, with 30KB of scanner
    output already in `messages` — and the checkpointer must
    preserve all of it across the resume.

- **7-E · Skill registry** (`src/rag/skills/registry.py`)
  - `@dataclass Skill(name, description, body, triggers: list[str],
    sandbox: SandboxCfg, max_iter: int, tools_allowed: list[str],
    tool_aliases: dict[str,str])`
  - `load_skills() -> dict[str, Skill]`: glob `data/skills/*.md`,
    split frontmatter (`yaml.safe_load`) from body, validate.
  - `match(query) -> Skill | None`: union-compile each skill's
    `triggers` regex; first match wins.
  - **Test:** drop a 3-line skill.md, assert it loads; bad YAML →
    skipped with a warning, doesn't crash startup.
  - **Learn:** "instructions as data" — the skill body is *content*,
    not code. Same pattern as `.claude/commands/*.md`,
    OpenAI Assistant `instructions`, Devin playbooks.

- **7-F · `skill_runner` node** (the agentic core — **use the
  prebuilt**, not hand-rolled)
  - Wraps `create_react_agent` (`prompt=` kwarg; moving to
    `langchain.agents.create_agent`). Around it the node owns
    **container lifecycle** for `mode: persistent` skills:
    ```python
    skill = SKILLS[state["skill_name"]]
    cid = state.get("skill_cid")            # survives interrupt() resume
    if skill.sandbox.mode == "persistent" and not cid:
        cid = DockerSandbox.start(skill.sandbox.image, repo,
                                  network=skill.sandbox.network,
                                  memory=skill.sandbox.memory,
                                  run_timeout=skill.sandbox.run_timeout)
    try:
        agent = create_react_agent(
            model=llm("agent", temperature=0),
            tools=build_skill_tools(skill, repo, cid),
            prompt=_preamble(skill, repo) + skill.body,
            checkpointer=get_checkpointer(),
        )
        out = agent.invoke({"messages": state["messages"]},
                           config={"recursion_limit": skill.max_iter * 2,
                                   "configurable": {"thread_id": ...}})
    except GraphInterrupt:
        # ask_user fired mid-run — keep the container alive,
        # checkpoint cid in state, re-raise so the parent graph
        # surfaces the interrupt. On resume we re-enter here with
        # cid already set and re-attach.
        return {"skill_cid": cid, ...re-raise...}
    finally:
        if cid and <run finished, not interrupted>:
            DockerSandbox.stop(cid)
    ```
  - `_preamble()` injects runtime facts the markdown can't know,
    **and varies by mode**:
    - `ephemeral`: "Each `run_shell` starts a FRESH container — no
      env/cwd/file carries between calls; write self-contained
      one-shot commands."
    - `persistent`: "All `run_shell` calls share ONE container for
      this run. Installed packages, files under `/work`, env vars,
      and cwd persist across calls. `/repo` is read-only — copy to
      `/work` before mutating. The container is destroyed at the
      end of the run."
    Both: "Emit your report via `write_report`. Phase 11 (git
    mutation) is DISABLED."
  - `build_skill_tools(skill, repo, cid)` returns the *narrowed*
    set (NOT `ALL_TOOLS`): `run_shell` (closes over either
    `(image, timeout, network)` for ephemeral or `(cid, timeout)`
    for persistent — the LLM only passes `cmd`), `read_file`,
    `grep`, `search_code`, `write_report`, `ask_user` (7-D),
    `git_log`. NOT `execute_code`, NOT `web_search`.
  - **No alias layer needed:** name the tools what the source
    skill expects — `@tool("run_shell_command")`,
    `@tool("grep_search")`. `write_todos` returns `"(tracked)"`.
  - **Lifecycle hardening:** `cid` is checkpointed in
    `state["skill_cid"]` so an `interrupt()` mid-run survives
    resume. A janitor (`DockerSandbox.reap("skill-",
    older_than=run_timeout)`) wired into FastAPI lifespan kills
    abandoned containers (user never resumed; server crashed).
  - Returns `{answer, messages, route="skill", skill_cid: None}`.
  - **Learn:** this is where "skill" stops being a config file and
    becomes behaviour. Same LLM, different prompt + narrowed
    toolset + longer leash → behaves like a different agent. And
    why the container lifecycle is the *node's* job, not the
    tool's — only the node knows when the run starts/finishes/
    pauses, so only it can `start`/`stop`/checkpoint `cid`.

- **7-G · Routing** (how a skill gets invoked)
  - Three entry paths, build all three:
    1. **Slash-style** in CLI/UI: `/security-audit acme-api` —
       parsed before the graph, sets `route="skill"` +
       `skill_name` + `repo_scope` directly. Cheapest.
    2. **Router regex**: `_SKILL_RE` compiled from
       `union(skill.triggers)` → "run a security audit on
       acme-api" → `route="skill"`. (Same shape as
       `_GIT_RE`/`_DOCS_RE`.)
    3. **As a tool**: `@tool run_skill(name, repo)` exposed to the
       *regular* `agent_loop`, so mid-conversation the agent can
       decide "this needs the audit skill" and invoke it. The tool
       calls `skill_runner` as a sub-invocation.
  - `graph.py`: `route_map["skill"] = "skill_runner"`; edge
    `skill_runner → respond` (skip evaluator — the report IS the
    deliverable; skip output_guard's abstain — there's no
    retrieval-score signal).
  - **Learn:** the three invocation patterns mirror how Claude Code
    surfaces skills (slash command, keyword trigger, agent-chosen).

- **7-H · Adapting the source skill** (`data/skills/security-audit.md`)
  - Take `comprehensive-security-audit/SKILL.md` and:
    - **Frontmatter:** `triggers`, `max_iter: 60`, `tools_allowed`,
      `sandbox: {mode: persistent, image: cto-secaudit:latest,
      timeout: 600, run_timeout: 1800, network: true, memory: 2g}`.
    - **Phase 0** → "Cross-stack scanners (semgrep/trivy/gitleaks/
      tfsec/checkov/bandit) and language toolchains (npm/pip/go/
      cargo/maven) are pre-installed. After Phase 1 discovers the
      stack, you MAY install stack-specific auditors (`gosec`,
      `brakeman`, `bundler-audit`, …) via the toolchain — they
      persist for this run. Do NOT use `brew`/`apt` (not
      available); use `pip install`/`npm install -g`/`go install`."
    - **Phase 2** → "Copy lockfiles to `/work` before resolving:
      `cp /repo/package*.json /work && cd /work && npm install &&
      npm audit --json`. `/repo` is read-only."
    - **Phase 11** → delete entirely; replace with "Emit a
      `## Suggested Remediation` section in the report."
    - **Final Delivery** → "call `write_report(repo,
      'YYYY-MM-DD-security-audit.md', content)`; then return ONLY
      the path + score + severity counts as your answer."
  - **Learn:** porting a host-execution playbook to a sandboxed
    server agent — what survives unchanged (Phases 1, 3–10), what
    moves into the persistent container (Phases 0, 2), what's
    dropped (Phase 11). Why git mutation is the line CTO won't
    cross even in persistent mode.

### What we deliberately do NOT build

- **Repo mutation** (Phase 11's `git checkout -b` / `commit` /
  `push`). CTO's repos are read-only sources of truth; mutating
  them breaks the watcher/indexer and the citation guarantee. The
  skill emits a fix-list; a human or CI applies it. Persistent
  mode doesn't change this — `/repo` stays `:ro`; only the
  container's own rootfs and `/work` are writable.
- **Host execution.** No `subprocess` on the CTO server, no
  devbox-on-host. The container *is* the boundary, in both modes.
- **Network in the sandbox by default.** `network: false` unless
  the skill opts in (security-audit needs it for runtime installs;
  a static-only skill wouldn't).
- **Container persistence across skill runs.** Persistent mode
  spans ONE skill invocation (start→stop). It is not a long-lived
  dev environment; "run security audit" twice = two fresh
  containers.

### Checkpoint

`run security audit on acme-api` →
- routes `skill`, no clarify
- `skill_runner` starts container `skill-<id>` (persistent mode)
- ~5–15 min, ~30–60 `⏺ run_shell(...)` calls — all `docker exec`
  against the same container; an `npm install` in iter 12 is
  visible to `npm audit` in iter 13
- mid-run `ask_user("Run npm install? (~3 min)")` → ❓ panel →
  resume → container survives, loop continues
- writes `data/reports/acme-api/<date>-security-audit.md`
- container `skill-<id>` is removed
- answer = "Saved report → <path>. Score 6.4/10. 2 CRITICAL · 8 HIGH · 14 MEDIUM."
- follow-up "what were the CRITICAL findings?" → normal agent route
  retrieves from the now-indexed report

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "Persistent vs. ephemeral — when is each the right default?" | The discover-then-act pattern vs. pure-function tools; what 'state across calls' actually buys; why ephemeral stays the default |
| "We turned on network + writable rootfs for persistent mode. What did we actually give up, and what's still protected?" | Threat-model layering: host-fs/caps/`/repo:ro` vs. `--read-only`/`--network=none`; container-local compromise vs. host compromise |
| "What happens when `ask_user` fires at iter 30 and the user closes the tab?" | Orphaned containers; why `cid` is checkpointed; the janitor pattern; `run_timeout` as a hard backstop |
| "Skill body as system prompt vs. as a planning document the agent reads via a tool — trade-offs?" | Prompt-stuffing vs. retrieval-augmented instructions; context-window cost; when each wins |
| "Why narrow the toolset per skill instead of giving every skill ALL_TOOLS?" | Capability bounding as a safety primitive; smaller tool surface → better tool selection |
| "`run_skill` as a tool the main agent can call — isn't that recursive agents?" | Yes. Sub-agent patterns; why depth-1 is usually enough; when you'd want a supervisor graph instead |
| "How would you let a skill *write* to the repo safely?" | Worktree isolation, PR-only writes, the Devin model; why CTO opts out even in persistent mode |
| "Where does this break at 50 skills?" | Trigger-regex collisions; skill discovery/listing; per-skill eval; versioning |
| "MCP servers vs. skills — same thing?" | No: MCP exposes *tools* (capabilities); skills compose tools into *procedures*. A skill might *use* an MCP-provided tool. |

---

## Phase 8 — Code Mutation: worktree primitives → `/superdev` → skill remediation

> **Status: ✅ done** (`phase-8`, 33/33 tests) — built as planned
> plus a **shell-only `/superdev`** variant (no repo arg →
> host_shell/read/docker_run/ask_user only, cwd=$PWD; surfaced
> from cloud-CLI use). Diverged: host_shell output cap 16 KB
> (50 KB caused 130 s+ hangs re-rendering large tables); inner
> agent streams tokens via `skill_event.text`. Architecture:
> `PHASE8_ARCHITECTURE.md`. Live PR + full Phase-11 run still to
> verify against a real remote.

**Goal:** let CTO write code — but only ever onto a disposable
git **worktree** branch that ships as a **PR for human review**.
Two consumers of the same primitives, built in this order:

1. **`/superdev`** — interactive, host-trusted, local-only. The
   user drives; CTO is a Claude-Code-style coding agent on your
   machine. Ships first: directly answers "why not just use
   Claude Code", and the host-side worktree is simpler than the
   container-mounted variant.
2. **Skill remediation** — autonomous. A skill (e.g.,
   `security-audit` Phase 11) edits inside its sandbox
   container's worktree mount and opens a PR. Ships second:
   reuses 8-A/8-B, adds the container-mount + `ask_user`
   consent. This is what unlocks Phase 9's scheduled fix-PRs.

**Learn:** worktree-as-sandbox (Devin, Claude Code `isolation:
"worktree"`); PR-as-permission-boundary; running two graphs with
disjoint capability sets; why mode-switching is a *user*
decision, never an LLM decision; the threat-model difference
between "server agent over indexed repos" (Phases 1–7) and
"local agent on the dev's own machine" (8-C).

### Shared primitives (build first; both consumers use them)

- **8-A · Worktree primitive** (`src/rag/vcs/worktree.py`)
  - `create(repo, branch_prefix) -> Worktree(path, branch)`:
    `git -C data/repos/<repo> worktree add <tmpdir> -b
    cto/<prefix>-<ts> HEAD`. Returns the host path + branch.
  - `discard(wt)`: `git worktree remove --force` + `git branch
    -D` (when no PR was opened).
  - `list_orphans()` / `reap()`: clean abandoned worktrees
    (server crash, user `/exit-superdev` without PR).
  - **Test:** `create()` → edit a file → `git -C wt.path diff`
    shows it; `data/repos/<repo>` is unchanged; `discard()`
    removes both dir and branch.
  - **Learn:** worktree vs clone vs Docker COPY — instant
    (<1s), shares `.git` objects, the *branch* is the only
    artifact. Why the source repo under `data/repos/` stays
    pristine (the watcher/indexer sees HEAD, never the WIP).

- **8-B · `create_pr` tool** (`src/rag/skills/tools.py`)
  - `@tool create_pr(repo, branch, title, body) -> str`:
    1. `git -C <wt> push -u origin <branch>`
    2. Detect host (`GIT_REMOTE_URL`): GitLab → POST
       `/projects/:id/merge_requests`; GitHub → `gh pr create`
       or REST. Returns the PR/MR URL.
  - **Refuses** branches named `main`/`master`/`develop`/
    `release*`. No `--force`. No merge.
  - Config: `GIT_TOKEN`, `GIT_REMOTE_URL` (env, never baked).
  - Commit-message convention enforced here:
    `<type>(<finding-or-area>): <desc>\n\nCo-Authored-By: CTO
    <noreply@cto.internal>`.
  - **Not** in `ALL_TOOLS` — must be explicitly added to a
    skill's `tools_allowed` or the superdev toolset.
  - **Learn:** PR-as-permission-boundary. CTO proposes;
    reviewer + CI are the gate; CTO never merges.

### Consumer 1 — `/superdev` mode (interactive, local)

- **8-C · The second graph + host toolset**

  **Activation gates (all required):**
  | Gate | Where |
  |---|---|
  | `CTO_SUPERDEV_ENABLED=true` (default off) | config.py — graph won't compile the route otherwise |
  | Local mode only — `--remote` and `app.py /query` refuse | cli.py / app.py |
  | Entered ONLY via user-typed `/superdev [repo]` | No router regex, no `match_skill`. The LLM never picks this. |
  | Visual indicator | CLI prompt `⚡› `, toolbar `SUPERDEV · cwd=…`; UI red banner |

  **`agents/superdev_graph.py`** — `build_superdev_graph()`:
  minimal `START → reset → input_guard → superdev_loop →
  respond → END`. No query_analysis/clarify/evaluator/cache/
  memories. `superdev_loop` = `create_react_agent` with the
  host toolset and a "you are a coding agent on the user's
  machine; ALWAYS confirm before destructive actions" prompt.

  **`agents/tools/host.py`:**
  - `host_shell(cmd, cwd=None)` — direct
    `subprocess.run(shell=True)`. cwd defaults to the session
    worktree (8-A). Output cap 50 KB. **Destructive-pattern
    detect** (`rm -rf`, `dd`, `mkfs`, force-push, `sudo`,
    writes to `/etc|/usr|~/.ssh|~/.aws`) → `ask_user` confirm.
  - `host_write(path, content)` / `host_edit(path, old, new)`
    — path-jailed to the session worktree + any dir the user
    added via `/superdev allow <dir>`.
  - `host_read(path)` — unrestricted read.
  - `docker_run(cmd, image, network=True)` — open-network
    container for when the user wants isolation but with
    internet (the inverse of the Phase-3 sandbox).
  - `ask_user`, `create_pr` (8-B).

  **Mode UX:** `/superdev <repo>` → 8-A creates a worktree,
  sets cwd, switches the active graph for this session.
  `/exit-superdev` → if uncommitted changes, `ask_user("PR,
  discard, or keep?")`; switch back to the read-only graph.

  **Checkpoint:**
  ```
  › /superdev acme-api
  ⚡ SUPERDEV · cwd=data/superdev/cli-…/acme-api (branch cto/superdev-…)
  ⚡› add a /healthz endpoint that returns the build version
  ⏺ host_read(.../HealthController.java)
  ⏺ host_edit(.../HealthController.java, …)
  ⏺ host_shell("./gradlew build")
    ⎿ BUILD SUCCESSFUL
  ⏺ ask_user("Open a PR?") → yes
  ⏺ create_pr(...) → https://gitlab/.../merge_requests/43
  ⚡› /exit-superdev
  ›
  ```

### Consumer 2 — Skill remediation (autonomous, sandboxed)

- **8-D · Worktree-in-container + skill Phase 11**

  Extend 8-A for the container path: `DockerSandbox.start(
  worktree=<wt.path>)` mounts the host-side worktree at
  `/work:rw` (instead of tmpfs); `/repo:ro` stays as the HEAD
  reference. The skill edits `/work`, runs `git -C /work
  commit`, calls `create_pr`. On `stop()` or failure → 8-A
  `discard()`.

  `SandboxCfg.worktree: bool` field; `tools_allowed:
  [..., create_pr]` in the skill frontmatter.

  **Skill body — Phase 11 (security-audit.md / a new
  `code-fix.md`):**
  ```markdown
  ## Phase 11 — Remediation (PR)
  ask_user("Found N CRITICAL + M HIGH. Open a fix PR?
            (CRITICAL only / CRITICAL+HIGH / skip)")
  For each finding in scope:
    1. Edit /work/<path> (NOT /repo)
    2. run_shell_command("cd /work && git add <f> && git commit -m '…'")
    3. run_shell_command("cd /work && <build-cmd>")
       fail → git revert HEAD → mark "manual fix needed"
  create_pr(repo, branch, title, body=<remediation table>)
  ```

  **Checkpoint:** `/security-audit acme-api` →
  Phases 0–10 (read-only, report) → `ask_user` consent →
  Phase 11 edits 2 files, build passes, `create_pr` →
  PR URL in answer + report link. Container removed; worktree
  kept (it's the PR branch).

### What we deliberately do NOT build

- **LLM-chosen entry to superdev.** Only the user's slash.
- **Server-side superdev.** Host-shell on a shared server is
  RCE-as-a-feature. `--remote` and `/query` refuse it.
- **Auto-merge / push to protected branches / `--force`.**
  Refused inside `create_pr`, not just discouraged in prompt.
- **`shell=True` without the destructive-pattern gate.** That
  list is the entire safety model for 8-C.
- **8-D before 8-C.** You can flip this if scheduled fix-PRs
  (Phase 9) matter more than interactive parity — both end at
  the same place; only the order of consumers differs.

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "Why a second graph instead of a `superdev` route in the main one?" | Compile-time capability isolation — host tools aren't even *importable* in the read-only graph. Defense in depth. |
| "Why path-jail to a worktree if the user already opted in?" | The user trusts *themselves*, not the LLM's argument generation. A prompt-injected file shouldn't redirect a write to `~/.ssh`. |
| "How is superdev different from just using Claude Code?" | Mostly it isn't — and that's fine. One tool does both knowledge-RAG and host-coding with a hard mode boundary the user controls. The differentiator lives in the read-only graph. |
| "Why open-network Docker here but not in Phase 3?" | Phase 3 runs against *indexed* (possibly untrusted) repos on a server. Superdev runs on *your* laptop against code *you're* writing. Different threat model → different defaults. |
| "Worktree on host (8-C) vs mounted into container (8-D) — why both?" | 8-C: dev iterating, wants instant feedback, trusts the box. 8-D: skill running headless/scheduled, must stay container-bounded. Same primitive, two trust contexts. |
| "Can I let CI auto-merge if tests pass?" | That's an org policy in GitLab/GitHub, not a CTO decision. CTO opens; the org's branch protection decides merge. |

---

## Phase 8.5 — `/superdev` as a RAG-aware coding assistant

> **Status: ✅ done** (`phase-8.5`, 39/39 tests). Unplanned —
> emerged from "make /superdev a write-focused coding
> assistant" and "can I drop github.com/obra/superpowers in
> as a skill?" (no — different runtime; but the *methodology*
> ports as playbooks). Architecture: `PHASE8_5_ARCHITECTURE.md`.

**Goal:** turn `/superdev` from "host shell with a PR exit"
into a coding agent that **reads like CTO and writes like
Claude Code** — RAG over the indexed corpus while editing a
worktree, with composable methodology playbooks and a live
checklist.

**Learn:** one-way capability import (superdev gains the
read-only graph's tools; the read-only graph gains nothing —
isolation holds); methodology-as-prompt-prefix vs
task-as-skill; the shared-mutable-list pattern for surfacing
in-tool state to the CLI between turns; why "the model marked
[N/N] done" is a prompt-design bug, not a tool bug.

### Deconstruction

- **8.5-A · RAG tools in superdev** — `_RAG_TOOL_NAMES` (12
  read-only tools) lazily imported from `TOOLS_BY_NAME` into
  `build_host_tools()`. The differentiator vs Claude Code:
  the agent can `find_callers("validateCSR")` *across all
  indexed repos* before changing a signature.

- **8.5-B · Playbooks** (`data/playbooks/*.md`,
  `agents/playbooks.py`) — pure methodology prose, no
  frontmatter, no tools. `render([names])` prepends to the
  superdev system prompt. Skill = *what* to do (sandboxed
  task with a report); playbook = *how* to do it (discipline
  on existing tools). Composable: `tdd plan-then-execute
  verify-before-done`. CLI: `/superdev <repo> [pb…]`,
  `⚡› /playbook <name>`, `/playbook off`, `/playbooks`.
  This is the port target for superpowers-style content:
  strip `Agent`/`Task`, keep the prose.

- **8.5-C · QoL tools** — `host_glob(pattern)` (recursive,
  jailed, .git-skipped); `host_apply_patch(diff)` (unified
  diff, every `+++` target jailed — multi-file refactors as
  ONE call); `todo(set|add|done|show)` (closes over a
  caller-owned list `sd["todos"]`; mutates in-place so the
  CLI toolbar reads `☑ N/M` between turns; emits
  `skill_event{kind:"todo",items}` → `_todo_panel` boxed
  checklist live in the trace).

- **Hard rules** baked into `_system_prompt` (apply with or
  without playbooks): never end a turn with uncommitted
  changes — last calls MUST be `commit` → `ask_user("Open a
  PR?")`; build exit 127 → say so and STILL commit+ask;
  prefer `host_glob`/`find_symbol` over `ls` chains. Each was
  a fix for an observed failure mode in the `/timestamp`
  test.

### Checkpoint

```
› /superdev acme-auth tdd plan-then-execute
⚡› change validateCSR to return Optional<Error> instead of throwing
⏺ todo(set, items=[…, "commit changes", "open PR"])
  ╭─ ☑ 0/6 ─────────────────────────╮
  │  · 1. find callers across repos  │  …
⏺ find_callers(name='validateCSR')          ← RAG, cross-repo
⏺ host_glob(pattern='**/*Test.java')
⏺ host_apply_patch(diff='--- a/…\n+++ b/…')  ← multi-file
⏺ host_shell('./gradlew test')               ← RED → GREEN
⏺ commit('refactor','crypto',…) → ask_user → create_pr
  ╭─ ☑ 6/6 ─╮
```

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "Why a separate `playbooks/` dir instead of reusing `skills/`?" | Skills have frontmatter, sandbox, narrowed `tools_allowed`, their own ReAct loop, and a `write_report` deliverable. Playbooks have none of that — they're a system-prompt prefix. Reusing the skill loader would mean special-casing every field to "ignore." |
| "Why mutate a shared list instead of putting todos in `AgentState`?" | The toolbar renders *between* turns when no graph is streaming. Reading `AgentState` would mean `graph.get_state()` on every prompt repaint. The list is process-local (superdev is local-only) and free to read. |
| "Why does `/playbook <name>` rebuild the whole graph?" | `create_react_agent`'s prompt is fixed at construction. Rebuilding is ~50 ms; the inner checkpoint thread holds conversation history, and `sd` holds wt/roots/todos, so only the prompt actually changes. |
| "Why playbook text BEFORE the base prompt?" | Models weight earlier instructions higher; "follow these STRICTLY" needs to win over "be concise" when they conflict. Putting it after would let the base prompt's brevity override the playbook's rigor. |
| "Why isn't `host_glob` in shell-only mode?" | It's jailed to `roots`, which in shell-only is `$PWD`. That's fine — but globbing arbitrary cwd without write tools is just `host_shell("find …")` with extra steps. Kept the no-write-tools-in-shell-only line clean. |
| "8.5-D subagents — what's hard?" | Each child needs its own worktree (or they clobber each other), its own checkpoint thread, and a way to stream N traces into one CLI. The `spawn(prompt)` tool is easy; the lifecycle/UX isn't. |

---

## Phase 8.5-G — wrap regular's quality pipeline around `/superdev`

> **Status: ✅ done** (`phase-8.5-G`, 10/10 existing tests still pass + 8 inline smoke checks).
> Built **as wrapper, not unification** — superdev keeps its own
> graph file, but each of the read-only graph's quality stages
> (`load_memories`, `evaluator`, `output_guard`, `summarize`,
> `save_memories`) is spliced around `superdev_loop`. Same node
> functions, two builders. Mode-aware so action turns (commit/
> edit/host_shell) skip eval + cache — the deliverable IS the
> change, not a gradable answer.

### What changed

| Layer | Change |
|---|---|
| Turn classification | NEW `agents/_turn_kind.py` — `classify_turn(new_msgs)` walks `AIMessage.tool_calls` against `READ_TOOLS` (12 native + ask_user/todo) + `READ_MCP_BASES` → `'read'\|'action'`. MCP `<server>__<tool>` prefix-stripped. |
| AgentState | `turn_kind: str` field added. |
| Wrapper wiring | `superdev_graph.py`: `reset → input_guard → load_memories → superdev_loop → _eval_route → (evaluator \| skip) → output_guard → respond → [should_summarize?] → summarize → save_memories → END`. **0 node reimplementations** — every stage IS the regular graph's node function. |
| Chunk extraction | `superdev_loop` post-stream calls `_extract_chunks(new)` over `search/search_code/search_docs` `ToolMessage`s (same pattern as `agent_loop`) so evaluator + output_guard see real chunks on read turns. |
| Evaluator skips | `nodes/evaluator.py`: skip superdev action; skip evidence-free turns (no chunks AND no `[SOURCE_N]`); force `will_retry=False` for `route='superdev'` (wrapper has no retry edge — kills phantom "retry #1" trace). |
| save_memories gate | `nodes/memories.py`: skip when `route='superdev' and turn_kind != 'read'` — prevents "Committed sha-abc" from being mined as a "durable user fact". |
| Tool-chain-safe summarize | `nodes/summarize.py`: `_safe_split(messages, keep_recent)` walks backward past `AIMessage(tool_calls)`↔`ToolMessage` chains so a summary never severs a pair → no more Anthropic "tool_use without tool_result" 400s. |
| System prompt | Added **Repo scope rule** (omit `repo=` unless user named one — fixed agent defaulting `repo='cto'` from cwd) + **`[SOURCE_N]` + footnote** convention for read turns (mirrors `agent_loop.py`). |
| Cache mode-scoping | `memory/cache.py`: `mode` kwarg threaded through `cache_lookup/cache_store/store_eligible/_point_id/_normalize`; UUID5 = `mode::query`; Qdrant server-side `Filter(must=[FieldCondition(mode)])` for superdev, `Filter(should=[…, IsEmptyCondition('mode')])` for regular (backward-compat legacy entries); `limit=3`; `store_eligible` rejects `route='superdev' and turn_kind!='read'`. |
| CLI critical fix | Loop var `mode, data = ev` SHADOWED the `_run_turn(..., mode='regular')` param → `cache_store(..., mode=mode)` was actually writing under `'custom'/'messages'/'updates'`, breaking BOTH regular AND superdev caching. Renamed to `kind, data = ev`. UI + app got same rename prophylactically. |
| CLI text overlay | `text` skill_event dropped `Padding(Markdown(streaming))` → `spin.note = "streaming (N chars)"` only. Superdev's inner agent echoed code/doc snippets while reasoning → flashed on screen. |
| Case-insensitive callers/callees | `retrieval/graph_query.py`: SQL `name = %(name)s` AND `e.callee_name = %(name)s` were case-sensitive (LLM-lowercased `validateauthtoken` missed indexed `validateAuthToken`). Added `case_sensitive=False` default + ILIKE. Tool wrappers expose the kwarg. Parity with `find_symbol`. |
| Stdout silencing | 30+ `print(...)` debug breadcrumbs across `nodes/{guards,evaluator,summarize,clarify,query_analysis,router,simple_rag}` + `memory/cache.py` → `log.debug/info/warning`. CLI bullets are the single source of trace output now. |

### Tradeoff that drove the design

**Unification** would have merged both graphs into one router-
branched StateGraph (~1140 LOC, 6 days). **Wrapper** keeps both
graph files, imports the same node functions in each (~260 LOC,
3.5 days). The wrapper's hazard is drift — someone adds a node
to regular and forgets to add it to superdev. Mitigation: this
phase doc lists the shared stages; one parity test
(`tests/test_pipeline_parity.py`) is the planned guard. Land
unification later if drift becomes a real cost.

### Cache key collision — the bug that made mode-scoping mandatory

Before: cache stored answers keyed only by the normalized query
text. Enabling cache on `/superdev` would mean a question like
"what does X do" — answered in superdev by *running* a command —
could be served verbatim from cache to the same paraphrase asked
in regular Q&A mode. Fix: prefix the UUID5 namespace with mode
(`mode::query`), plus payload `mode` field + server-side Qdrant
filter. Backward-compat for pre-8.5-G entries: regular's filter
ORs in `IsEmptyCondition` so entries without the `mode` field
still match a regular lookup.

### Evaluator's three new skip cases

Read these together — the order matters:

1. **Action turn** (`route='superdev' and turn_kind='action'`): no answer to grade against retrieval; the change IS the deliverable.
2. **Evidence-free turn** (no chunks AND no `[SOURCE_N]` in answer, AND not the explicit "nothing found" pattern): conversational/meta replies like "why is X scoped to Y?" — grading invents groundedness against an empty context.
3. **Suppress `refine_hint` for superdev**: the wrapper has no retry edge. Without this, the CLI's evaluator handler renders `⏺ retry #N — <hint>` for a retry that never happens.

### The "always defaults to cwd's repo" bug

Symptom: agent always passed `repo='cto'` to
`find_callers` even when the user asked about a symbol in
`auth-service`. Cause: the system prompt mentioned
`cwd: …/cto` and the agent extrapolated. Fix: an
explicit "Repo scope rule" paragraph in the prompt — *omit
`repo=` by default; pass only when user named one or after
disambiguation*. Combined with the case-insensitive
`find_callers/callees` fix, lowercase queries in /superdev now
return the same callers regular mode does.

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "Why not put `_extract_chunks` in a shared helper?" | It exists in 3 copies (`agent_loop` hand-rolled + prebuilt, plus superdev). Should be one. Tagged as a follow-up; the wrapper PR was the wrong place to add a new shared module. |
| "Why did the cache regress *both* regular AND superdev?" | The function param `mode` and loop var `for mode, data in ev` collided. Python doesn't warn on shadowing. Caught only after the code review angle "removed-behavior auditor" traced cache_store args back to their definition. Rename + add `# NOTE: don't shadow…` comment. |
| "Why is `_safe_split` a tool-chain protector instead of `trim_messages`?" | langchain_core has `trim_messages(strategy='last', allow_partial=False)` which would do this. Local copy is an interim; lifting to the langchain API is a clean follow-up that benefits both graphs. |
| "Why does superdev's evaluator skip when no `[SOURCE_N]` AND no chunks?" | Grading a conversational reply (`"why is X scoped to Y?"`) against an empty context invents groundedness scores and emits a refine_hint pointing at nothing. The skip catches "agent answered from prior turn / parametric knowledge" — those aren't gradable RAG outputs. |
| "Why do I need a separate `turn_kind` instead of inspecting `messages`?" | classify_turn DOES inspect messages — but downstream nodes (evaluator, store_eligible) shouldn't re-walk the list. Caching the verdict on `AgentState` makes the contract explicit and one-pass per turn. |
| "What's the drift risk with two graph files?" | When someone adds a node to `graph.py` (say a future `judge_pii_in_memory`) and forgets `superdev_graph.py`, superdev silently lacks the new safety. Mitigation: parity test asserts both graphs include the canonical stage set or explicitly opt out with a comment. |
| "Should mode become an Enum?" | Yes. Right now `'regular'` / `'superdev'` are bare strings. A `Literal['regular','superdev']` or module-level constants would catch typos at type-check time. Bundled as follow-up cleanup. |

---

## Phase 9 — Task Scheduler (skills on a clock)

> **Status: ✅ done** (`phase-9`, 29/29 tests). Built as planned
> **plus**: a third `exec:` job kind (run a checked-in script —
> sandbox or host-flag-gated — for ops jobs like git-pull-all /
> EC2-cost-to-Slack that need creds but no LLM); `SandboxCfg
> .env_passthrough/mounts` so persistent skills get *scoped*
> creds without host shell; an HTML dashboard at `GET /jobs`
> (Accept-negotiated, ▶fire, runs table, log tail) and a Gradio
> `⏰ Jobs` modal; per-fire log file `data/logs/scheduler.log`.
> Built-in 5-field cron (no croniter dep). Architecture:
> `PHASE9_ARCHITECTURE.md`. Rejected: scheduled `/superdev`
> (headless host_shell = §8 RCE-as-a-feature non-goal).

**Goal:** run skills (and arbitrary CTO queries) on a schedule —
nightly security audits, weekly dependency-CVE sweeps, daily
`release-notes` since the last tag — without a human typing the
slash command. Reports accumulate in `data/reports/` and are
indexed; deltas can be diffed against the previous run.

**Learn:** generalizing the existing `ConfluenceScheduler`
(connectors/scheduler.py — a daemon thread that wakes, checks
`_due_spaces()`, syncs, sleeps) into a job runner that invokes
the graph instead of an HTTP client. Cron-vs-interval semantics;
idempotency; what "headless graph invoke" needs that interactive
doesn't (no `interrupt()`, deterministic session IDs, structured
output).

### Deconstruction

- **9-A · Job spec** (`data/schedules.yaml`)
  ```yaml
  jobs:
    - name: nightly-secaudit
      cron: "0 2 * * *"            # 02:00 daily
      skill: security-audit
      repos: [acme-api, acme-auth, acme-mobile]
      on_complete: [index, diff_prev]
    - name: weekly-deps
      cron: "0 6 * * 1"            # Mon 06:00
      skill: dependency-audit
      repos: "*"                   # every indexed repo
    - name: hourly-question
      interval_min: 60
      query: "any new CRITICAL findings since the last audit?"
      notify: slack://#sec-alerts   # Phase-9-D
  ```
  Schema: `name`, `cron|interval_min`, `skill|query`,
  `repos|scope`, `enabled`, `on_complete: [index, diff_prev,
  notify]`, `headless: true` (default — see 9-C).

- **9-B · `SkillScheduler`** (`src/rag/scheduler.py`)
  - Generalize `ConfluenceScheduler`: one daemon thread,
    `croniter` (or `schedule` lib) for next-fire computation,
    `_due_jobs()` → for each, spawn a worker thread that does
    `graph.invoke({query, skill_name, repo_scope},
    config={"thread_id": f"sched/{job}/{ts}"})`.
  - Persist run history to Postgres (`scheduled_runs`:
    job, started_at, finished_at, status, report_path,
    answer_summary) so `diff_prev` and the `/jobs` listing have
    state. Reuses the existing `get_conn()`.
  - `start_if_configured()` wired into FastAPI lifespan exactly
    like `connectors/scheduler.py:start_if_configured`.
  - **Test:** a `interval_min: 1` job with `query: "what repos
    are indexed?"` fires, writes a `scheduled_runs` row, answer
    appears in `make jobs-log`.

- **9-C · Headless mode** — the hard part
  - Scheduled runs have **no user** to answer `interrupt()`. So:
    - `state["headless"] = True` in the invoke payload.
    - `clarify` node: when headless and would interrupt → log
      "ambiguous, picking ALL candidates" and continue (or skip
      the job with a recorded failure reason).
    - `ask_user` skill tool: when headless → return the job's
      configured default (`headless_answers: {…}` in the spec)
      or `"skip"` and log it.
  - `SkillScheduler` enforces a hard wall-clock per job (the
    skill's own `run_timeout` + a margin) so a hung run doesn't
    block the next fire.
  - **Learn:** every interactive affordance (clarify, ask_user,
    cache-hit-seed) has a headless degenerate. Designing for
    "no human in the loop" is a different shape than "human
    sometimes in the loop."

- **9-D · Notification + diff hooks** (`on_complete`)
  - `index` — already done by `write_report` → no-op confirm.
  - `diff_prev` — `difflib` the new report against the most
    recent prior `scheduled_runs.report_path` for the same
    job; emit a `…-delta.md` highlighting new/resolved
    findings.
  - `notify` — pluggable: `slack://`, `email://`, `jira://`
    (create a ticket per new CRITICAL). Start with a logger
    that prints what *would* be sent.

- **9-E · Surfaces**
  - `GET /jobs` — list jobs + last-run status + next-fire.
  - CLI `/jobs` — same panel.
  - `make jobs-run JOB=nightly-secaudit` — fire one now
    (testing).
  - UI 📚 Sources modal gains a "Scheduled" section.

### What we deliberately do NOT build

- **Distributed scheduling.** One process, one host. K8s
  CronJob / Celery / Temporal are the right answer at scale
  (Phase-6's deferred EKS plan); this is the single-box
  precursor.
- **At-most-once / exactly-once.** If the server restarts
  mid-job, the job re-runs next fire. Reports are idempotent
  (date-named, overwrite), so this is acceptable.
- **Job DAGs.** No "run B after A succeeds." One job, one
  schedule.

### Checkpoint

`data/schedules.yaml` with a `nightly-secaudit` job →
`make serve` → at 02:00 the lifespan thread fires →
`data/reports/<repo>/<date>-security-audit.md` appears for each
repo → `…-delta.md` shows 1 new HIGH since yesterday →
`/jobs` shows last-run ✓ + next-fire 02:00 tomorrow.

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "Why a daemon thread instead of host cron + `make ask`?" | State (run history, diff_prev) lives in the process; the graph is already warm; one fewer system to configure. Cron + curl-the-API is a valid alternative for prod. |
| "What happens when two jobs target the same repo at the same time?" | Persistent skill containers are per-job-instance (`cto-skill-<uuid>`), so no conflict. The reports overwrite by date — last-write-wins. If that's wrong, add `<job>` to the report filename. |
| "How does headless interact with the evaluator retry?" | Same as interactive — evaluator doesn't `interrupt()`. The risk is a retry loop on a flaky gateway burning the run_timeout; the per-job wall-clock is the cap. |
| "Openclaw / cron / Temporal — when do you graduate?" | When you need multi-host, retries-with-backoff, job DAGs, or an ops UI. The daemon-thread scheduler is the 80% case for one team. |

---

## Phase 10 — Distribution: binary CLI + setup wizard

> **Status: ✅ done** (`phase-10`, smoke-tested; build not run live).
> Turn `cto` from a `pip install -e .` developer tool into an
> end-user product: one downloaded binary, wizard-driven config,
> optional Docker bootstrap of the backing infra. Distribution
> = offline (internal artifact share). macOS-first (arm64 +
> x86_64); Linux/Windows deferred.

### What changed

| Layer | Change |
|---|---|
| **New `src/rag/cli/` package** | `_config.py` (XDG layout, atomic write, 0600 secrets), `_health.py` (stdlib-only probes + `redact()`), `setup.py` (wizard), `doctor.py` (diagnostics), `compose.py` (docker compose driver) |
| **Subcommand routing** | `api/cli.py:main()` dispatches `setup`/`doctor`/`compose` BEFORE the heavy chat-REPL argparse — sub-200ms cold start on those paths |
| **Bundled compose** | `packaging/compose.cto-infra.yaml` (qdrant + postgres + phoenix; `${CTO_DATA_DIR}` templated; per-svc healthchecks). Extracted to `$XDG_DATA_HOME/cto/compose/` on first call |
| **Binary harness** | `packaging/pyinstaller_cto.spec` (`--onefile` thin-client; ~650 MB of ML/server deps excluded → 30-50 MB binary). Makefile `binary/binary-mac-{arm64,x86_64}/binary-clean` |
| **pyproject** | `[binary]` extras = `pyinstaller>=6.0` |

### XDG layout

| Path | Contents | Perms |
|---|---|---|
| `$XDG_CONFIG_HOME/cto/config.yaml` | mode, server URL, prefs | 644 |
| `$XDG_CONFIG_HOME/cto/credentials.toml` | API keys + tokens | **600** |
| `$XDG_DATA_HOME/cto/compose/docker-compose.yaml` | extracted from binary | 644 |
| `$XDG_DATA_HOME/cto/{qdrant,postgres,phoenix}/` | bind-mount targets | 755 |
| `$XDG_STATE_HOME/cto/` | session history | 755 |

`.env` files are dev-only. End-user binary never reads `.env`.

### Subcommand surface

```
cto setup              # single-pass interactive wizard (full re-run)
cto doctor [--unsafe]  # redacted-by-default connectivity report
cto compose pull|up|down|ps|logs|status
cto chat [...]         # unchanged
cto [question]         # unchanged
```

### Tradeoffs locked in

| Tradeoff | Decision | Why |
|---|---|---|
| Thin client vs fat bundle | **Thin** | Fat = ~700 MB (torch + transformers); thin = 30-50 MB |
| Bundle cto-app server in compose | **No** | Needs registry agreement; v1 = infra only |
| Distribution channel | **Offline (internal share)** | User direction — no GitHub Releases, no Homebrew, no auto-update |
| Build matrix | **macOS arm64 + x86_64 only** | User direction — Linux/Windows later |
| Local-mode store fallbacks | **Docker required** | User direction — no native-binary Qdrant/Postgres path |
| Wizard re-entry | **Single full re-run** | User direction — defaults pre-filled so re-run is mostly Enter |
| Per-user namespacing | **Single shared `cto` compose project** | User direction — defer to multi-tenant sprint |

### Discussion Hooks

| Ask me | What you'll learn |
|---|---|
| "Why XDG instead of `~/.cto/`?" | XDG separates config (small, backed up) from data (big, regenerable) from state (per-session, often ignored). Aligned with how `gh`, `claude`, `helm` lay out. The dotfile convention loses you that separation and dumps everything in one place. |
| "Why a custom `_health.py` instead of httpx?" | The binary stays thin only if we don't pull httpx/aiohttp/anyio into the bundle for one feature. Stdlib `urllib.request` covers HTTP probes; falls back to TCP open-port for postgres when psql isn't on PATH. Saves ~3 MB. |
| "Why atomic write via `.tmp` + `os.replace`?" | Ctrl-C mid-wizard could leave half a YAML file. `os.replace` is atomic on POSIX even across crashes; the user's config either has the old values or the new ones, never garbage. Same trick git uses for index updates. |
| "Why `<existing>` sentinel for secrets?" | Re-running the wizard, the user shouldn't have to re-type their API key. Blank-default would mean "clear this secret" — ambiguous. Sentinel makes the intent explicit; we never echo the actual secret back. |
| "Why poll `docker compose ps --format json` instead of probing services directly?" | Compose's healthcheck verdict is the source of truth; duplicating probe logic risks divergence. Plus compose tells us *why* a container is unhealthy (exit code, last log line) — our probes only say "yes/no". |
| "Why hand-roll dispatch in compose.py instead of argparse?" | Startup time. Each argparse subparser adds ~3-5 ms cold; with ~6 subcommands that's noticeable on the binary's ~150 ms baseline. Hand-rolled = O(1) string match. |
| "Why excludes instead of includes in the PyInstaller spec?" | PyInstaller's static import analyzer crawls everything reachable from the entry point. Switching to an allowlist (`hiddenimports` only) would mean enumerating every transitive — fragile. Excludes are surgical and survive refactors. |
| "Why redact-by-default in doctor instead of opt-in?" | Output gets pasted in Slack channels. Redaction is the safe failure mode; the user has to deliberately type `--unsafe` to opt out. Defaults matter when humans are tired. |

---

## Phase 11 — TBD (next discussion: Sprint 1 skills hardening OR continuous improvement)

---

## LiteLLM Integration (Cross-Cutting)

LiteLLM is not a phase — it's present from Phase 0 onward.

### litellm_config.yaml (dev)

```yaml
model_list:
  - model_name: router-haiku
    litellm_params:
      model: anthropic/claude-haiku-4-5-20251001
  - model_name: agent-sonnet
    litellm_params:
      model: anthropic/claude-sonnet-4-6-20250514
  - model_name: agent-opus
    litellm_params:
      model: anthropic/claude-opus-4-7-20250415
  - model_name: embed
    litellm_params:
      model: voyage/voyage-code-3
  - model_name: rerank
    litellm_params:
      model: cohere/rerank-v3.5

router_settings:
  fallbacks: [{"agent-sonnet": ["agent-opus"]}]
  num_retries: 2

general_settings:
  master_key: sk-dev-master
```

### litellm_config.yaml (prod)

```yaml
model_list:
  - model_name: router-haiku
    litellm_params:
      model: bedrock/anthropic.claude-haiku-4-5-20251001-v1:0
      aws_region_name: us-east-1
  - model_name: agent-sonnet
    litellm_params:
      model: bedrock/anthropic.claude-sonnet-4-6-20250514-v1:0
      aws_region_name: us-east-1
  - model_name: agent-opus
    litellm_params:
      model: bedrock/anthropic.claude-opus-4-7-20250415-v1:0
      aws_region_name: us-east-1
  - model_name: embed
    litellm_params:
      model: voyage/voyage-code-3
  - model_name: rerank
    litellm_params:
      model: cohere/rerank-v3.5

router_settings:
  fallbacks: [{"agent-sonnet": ["agent-opus"]}]
  num_retries: 3
  routing_strategy: latency-based-routing

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

### App code (identical dev & prod)

```python
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

def llm(model: str = "agent-sonnet") -> ChatOpenAI:
    return ChatOpenAI(
        base_url=settings.LITELLM_URL,  # http://localhost:4000 or http://litellm.svc
        api_key=settings.LITELLM_KEY,
        model=model,
        streaming=True,
    )

def embedder() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        base_url=settings.LITELLM_URL,
        api_key=settings.LITELLM_KEY,
        model="embed",
    )
```

---

## How We Work Together

| You say | I do |
|---|---|
| `"Phase N — let's discuss"` | Explain concepts, draw diagrams, surface key decisions *before* code |
| `"Phase N — alternatives for <X>"` | Compare 2–4 options with trade-off table, recommend, let you pick |
| `"Phase N — implement <component>"` | Write the code, explain non-obvious choices as we go |
| `"Phase N — I'm stuck on <X>"` | Debug together; ask for error/trace, reason about root cause |
| `"Phase N — checkpoint"` | Verify deliverables work; run eval; decide if ready for N+1 |
| `"Why did we choose <X> in Phase M?"` | Recall reasoning from this doc + memory |
| `"Go back to Phase M"` | No phase is locked — revisit any time |

---

## Quick Reference: What's Learned Where

| Concept | Phase |
|---|---|
| Embeddings & similarity | 0 |
| Eval-driven development | 0, all |
| tree-sitter AST parsing | 1 |
| Hybrid retrieval (dense+BM25+RRF) | 1 |
| Cross-encoder reranking | 1 |
| LangGraph StateGraph basics | 2 |
| Conditional routing | 2 |
| PostgresSaver (short-term memory) | 2 |
| Streaming (SSE) | 2 |
| Inline citations | 2 |
| LiteLLM proxy | 0–all |
| Code graph / structural retrieval | 2.5 |
| Subgraphs / multi-agent supervisor | 3 |
| Sandboxing (E2B) | 3 |
| Prompt-injection defense | 3 |
| Long-term memory (Store) | 4 |
| Semantic caching | 4 |
| Send API / fan-out parallelism | 4 |
| Human-in-the-loop (interrupt) | 4 |
| State reducers for parallel merge | 4 |
| LLM-as-judge / self-correction | 4.5 |
| Tracing / observability (Phoenix, OTel) | 4.6 |
| Confluence connector (version-diff sync) | 5 |
| Tiered retrieval cascade (early-exit) | 5 |
| On-demand tools (git history, JIRA) | 5 |
| Cross-source join (ticket→commit→wiki→code) | 5 |
| Relatedness bands / general-knowledge answers | 5 |
| Webhook-driven indexing | 5 (deferred) |
| MCP server | 5 (deferred) |
| Slack integration | 5 (deferred) |
| Kubernetes / KEDA / HPA | 6 |
| IaC (Terraform) | 6 |
| Security hardening | 6 |
| Cost optimization | 6 |
| HyDE / RAPTOR / fine-tuning | 7 |
