# Phase 2 — Architecture & Code Flow

> LangGraph StateGraph: router → {simple_rag | agent_loop} → respond. PostgresSaver checkpointer for multi-turn. FastAPI + SSE streaming. Tools: search_code, read_file, grep, web_search.

## 1. System Architecture

```mermaid
flowchart LR
    subgraph Clients
        cli[scripts/ask.py<br/>CLI]
        http[curl / browser<br/>SSE]
    end

    subgraph API
        fapi[api/app.py<br/>FastAPI<br/>POST /query]
    end

    subgraph Graph["LangGraph StateGraph"]
        direction TB
        router[router<br/>gpt-4.1-mini<br/>+ heuristic]
        srag[simple_rag<br/>Phase 1 single-shot]
        aloop[agent_loop<br/>ReAct: hand-rolled<br/>or create_react_agent]
        resp[respond<br/>extract SOURCE_N]
        router -->|simple| srag
        router -->|agent| aloop
        srag --> resp
        aloop --> resp
    end

    subgraph Tools["Agent Tools"]
        sc[search_code]
        rf[read_file]
        gp[grep]
        ws[web_search]
    end

    subgraph Retrieval
        hyb[hybrid.py<br/>dense+sparse+RRF]
        rr[reranker.py<br/>bge-reranker-v2-m3]
    end

    subgraph Storage
        qdrant[(Qdrant<br/>code collection)]
        pg[(Postgres<br/>checkpoints,<br/>checkpoint_blobs,<br/>checkpoint_writes)]
        repos[(data/repos/)]
        vfile[(data/vocab.json)]
    end

    subgraph External
        litellm[LiteLLM Gateway<br/>corp LiteLLM proxy]
        tav[Tavily API]
    end

    cli --> Graph
    http --> fapi
    fapi --> Graph

    Graph -.checkpointer.-> pg

    aloop --> sc
    aloop --> rf
    aloop --> gp
    aloop --> ws

    sc --> hyb
    sc --> rr
    srag --> hyb
    srag --> rr
    hyb --> qdrant
    hyb --> vfile

    rf --> repos
    gp --> repos
    ws --> tav

    router --> litellm
    srag --> litellm
    aloop --> litellm
    hyb -->|embed| litellm
```

## 2. StateGraph Flow

```mermaid
flowchart TD
    start([START]) --> R

    R[router<br/>reads: query, messages<br/>returns: route]
    R --> dec{route?}

    dec -->|simple| S[simple_rag<br/>hybrid_search → rerank<br/>→ single Claude call<br/>returns: answer, retrieved_chunks,<br/>messages, iteration=1]

    dec -->|agent| A[agent_loop<br/>ReAct: bind_tools → invoke<br/>→ execute tool_calls<br/>→ loop until no tool_calls<br/>or MAX_ITER=8<br/>returns: answer, retrieved_chunks,<br/>messages delta, iteration]

    S --> P[respond<br/>regex SOURCE_N<br/>→ map to retrieved_chunks<br/>returns: citations]
    A --> P
    P --> done([END])

    pg[(PostgresSaver<br/>thread_id = session_id)]
    R -.checkpoint.-> pg
    S -.checkpoint.-> pg
    A -.checkpoint.-> pg
    P -.checkpoint.-> pg

    style R fill:#fff3e0,color:#000
    style S fill:#e8f5e9,color:#000
    style A fill:#e3f2fd,color:#000
    style P fill:#fce4ec,color:#000
```

## 3. Agent Loop Sequence (ReAct, hand-rolled)

```mermaid
sequenceDiagram
    actor User
    participant API as api/app.py
    participant G as graph.py
    participant R as router
    participant AL as agent_loop
    participant L as LiteLLM<br/>(claude-sonnet-4-6)
    participant T as tools/*
    participant Q as Qdrant
    participant PG as Postgres<br/>(checkpointer)

    User->>API: POST /query {q, session_id}
    API->>G: stream({query, messages:[Human(q)]},<br/>thread_id=session_id)
    G->>PG: load state for thread_id
    PG-->>G: prior messages (if any)

    G->>R: state
    R->>R: history_len>1 + referential? → agent<br/>else → LLM classify
    R-->>G: {route: "agent"}
    G->>PG: checkpoint
    API-->>User: SSE: {node:"router", route:"agent"}

    G->>AL: state (full message history)

    loop max 8 iterations
        AL->>L: invoke([System, ...history, Human(q), ...])<br/>tools=[search_code, read_file, grep, web_search]
        L-->>AL: AIMessage(tool_calls=[...]) or content
        alt tool_calls present
            loop each tool_call
                AL->>T: tool.invoke(args)
                T->>Q: hybrid_search / read / grep
                Q-->>T: results
                T-->>AL: result string
                AL->>AL: append ToolMessage(<retrieved>...)
            end
        else no tool_calls (final answer)
            AL->>AL: break
        end
    end

    AL-->>G: {messages: new_msgs, answer, retrieved_chunks, iteration}
    G->>PG: checkpoint
    API-->>User: SSE: {node:"agent_loop", tool_calls, answer}

    G->>G: respond → extract citations
    G->>PG: checkpoint
    API-->>User: SSE: {event:"done", answer, citations}
```

## 4. Data Anatomy — AgentState

```mermaid
flowchart LR
    subgraph state["AgentState (TypedDict)"]
        direction TB
        m["messages: Annotated[list, add_messages]<br/>Full conversation: Human, AI, Tool messages<br/>Persisted across turns by checkpointer"]
        q["query: str<br/>Current turn's question (overwritten each turn)"]
        rt["route: str<br/>'simple' | 'agent'"]
        rc["retrieved_chunks: Annotated[list, operator.add]<br/>Search results this turn (parallel-merge safe)"]
        a["answer: str<br/>Final response text"]
        c["citations: list[dict]<br/>[{ref, file, symbol, snippet}]"]
        i["iteration: int<br/>Tool-loop count (guard)"]
    end

    subgraph reducers["Reducers (how deltas merge)"]
        am["add_messages:<br/>append + dedup by msg.id"]
        oa["operator.add:<br/>list concatenation"]
        lw["(default):<br/>last write wins"]
    end

    m -.-> am
    rc -.-> oa
    q -.-> lw
    rt -.-> lw
    a -.-> lw
    c -.-> lw
    i -.-> lw
```

**Checkpointer storage (Postgres):**

```
checkpoints           — one row per (thread_id, checkpoint_id); state snapshot pointer
checkpoint_blobs      — serialized state values (msgpack)
checkpoint_writes     — pending writes between nodes
checkpoint_migrations — schema version
```

## 5. Module Dependency Graph

```mermaid
flowchart TD
    cfg[config.py<br/>+ POSTGRES_DSN, REPOS_DIR,<br/>AGENT_PREBUILT, AGENT_MAX_ITER]

    emb[embed.py]
    cfg --> emb

    subgraph retrieval
        hyb[hybrid.py<br/>lazy vocab + client<br/>+ payload filters]
        rr[reranker.py]
    end
    cfg --> hyb
    emb --> hyb

    subgraph agents
        st[state.py]
        lf[llm.py]
        gr[graph.py]

        subgraph nodes
            nrt[router.py]
            nsr[simple_rag.py]
            nal[agent_loop.py]
            nrs[respond.py]
        end

        subgraph tools
            tsc[search_code.py]
            trf[read_file.py]
            tgp[grep.py]
            tws[web_search.py]
        end
    end

    cfg --> lf
    cfg --> nal
    cfg --> trf
    cfg --> tgp
    cfg --> tws

    lf --> nrt
    lf --> nsr
    lf --> nal

    hyb --> tsc
    rr --> tsc
    hyb --> nsr
    rr --> nsr

    tools --> nal
    st --> gr
    nrt --> gr
    nsr --> gr
    nal --> gr
    nrs --> gr

    subgraph memory
        ckpt[checkpointer.py<br/>PostgresSaver]
    end
    cfg --> ckpt
    ckpt --> gr

    subgraph api
        app[app.py<br/>FastAPI + SSE]
    end
    gr --> app

    ask[scripts/ask.py]
    gr --> ask

    style gr fill:#e3f2fd,color:#000
    style app fill:#e1f5e1,color:#000
    style ask fill:#e1e5f5,color:#000
```

## 6. Phase 1 vs Phase 2 — What Changed

| Aspect | Phase 1 | Phase 2 |
|---|---|---|
| Execution model | Script: `query_v1.py` runs once, exits | Service: LangGraph StateGraph, stateful, always-on |
| Entry point | `make query-v1 Q="..."` | `make ask Q="..." S=sid` (CLI) or `make serve` + `POST /query` (HTTP/SSE) |
| Routing | None — always full retrieve→rerank→generate | `router` node: heuristic + gpt-4.1-mini → `simple` or `agent` |
| Retrieval strategy | Fixed: hybrid → rerank → top-8 → stuff prompt | Agent decides: search_code (multiple times), read_file, grep, web_search |
| Iteration | 1 LLM call | 1 (simple) or up to 8 (agent loop) |
| Multi-turn memory | None | PostgresSaver checkpointer keyed by `thread_id` |
| Citations | `[N]` ad-hoc | `[SOURCE_N]` extracted to structured `citations[]` |
| Streaming | Print at end | SSE events per node (router → tool calls → answer → done) |
| Tool implementations | n/a | 4 tools: search_code (wraps Phase 1), read_file, grep (rg), web_search (Tavily) |
| `hybrid.py` | Vocab passed by caller, eager Qdrant connect | Self-contained: lazy vocab + client, repo/layer payload filters |
| New deps | — | langgraph, langchain-openai, langchain-core, langgraph-checkpoint-postgres, psycopg, fastapi, uvicorn, sse-starlette, tavily-python |
| Lines added | — | ~700 across 16 new files |

### Known issues found during checkpoint

| Issue | Fix applied |
|---|---|
| Router ignored conversation history → follow-ups routed `simple` → wrong retrieval | Router now reads `state["messages"]`; heuristic short-circuit (referential language + history → `agent`); LLM classifier sees prior turns |
| `agent_loop` returned full history instead of delta → `add_messages` would duplicate | Returns only `messages[prior_count:]` |
| Hardcoded "acme codebase" / enrollment-specific examples | Genericized in system prompts |
