# Phase 2.5 — Architecture & Code Flow

> Code graph: tree-sitter call extraction → Postgres (`code_symbols`, `code_edges`) → recursive-CTE queries → `find_symbol` / `find_callers` / `find_callees` agent tools. Adds **structural** retrieval alongside Phase 1's semantic+keyword retrieval.

## 1. System Architecture

```mermaid
flowchart LR
    subgraph Sources
        repos[(data/repos/)]
    end

    subgraph Indexing["Indexing (offline)"]
        idxv[index_repos_v1.py<br/>chunks → Qdrant]
        idxg[index_graph.py]
        cg[code_graph.py<br/>tree-sitter parse<br/>extract symbols + calls<br/>resolve_edges]
        idxg --> cg
    end

    subgraph Storage
        qdrant[(Qdrant<br/>dense + sparse)]
        pg[(Postgres)]
        pgGraph[code_symbols<br/>code_edges]
        pgCkpt[checkpoints*]
        pg --- pgGraph
        pg --- pgCkpt
    end

    subgraph Retrieval
        hyb[hybrid.py<br/>vector + RRF]
        rr[reranker.py]
        gq[graph_query.py<br/>recursive CTEs]
    end

    subgraph Graph["LangGraph Agent"]
        router[router<br/>+ structural regex]
        srag[simple_rag]
        aloop[agent_loop]
        router --> srag
        router --> aloop
    end

    subgraph Tools["Agent Tools (7)"]
        sc[search_code]
        rf[read_file]
        gp[grep]
        ws[web_search]
        fs[find_symbol]
        fcr[find_callers]
        fce[find_callees]
    end

    repos --> idxv
    repos --> idxg
    idxv --> qdrant
    cg --> pgGraph

    aloop --> sc
    aloop --> rf
    aloop --> gp
    aloop --> ws
    aloop --> fs
    aloop --> fcr
    aloop --> fce

    sc --> hyb
    sc --> rr
    srag --> hyb
    srag --> rr
    hyb --> qdrant

    fs --> gq
    fcr --> gq
    fce --> gq
    gq --> pgGraph

    rf --> repos
    gp --> repos

    Graph -.checkpointer.-> pgCkpt

    style fs fill:#ffe0b2,color:#000
    style fcr fill:#ffe0b2,color:#000
    style fce fill:#ffe0b2,color:#000
    style gq fill:#ffe0b2,color:#000
    style cg fill:#ffe0b2,color:#000
    style pgGraph fill:#ffe0b2,color:#000
```

## 2. Graph Indexing Flow

```mermaid
flowchart TD
    start([data/repos/]) --> setup[setup_schema + reset_graph<br/>TRUNCATE code_symbols, code_edges]

    setup --> repo{for each repo}
    repo --> file{for each file<br/>ext in CALL_PATTERNS}

    file --> parse[tree-sitter parse → AST]
    parse --> imports[extract imports<br/>import_declaration / use / #include<br/>→ short_name → FQN map]
    parse --> walk[walk AST for SYMBOL_TYPES]

    walk --> sym["emit Symbol<br/>name = child_by_field_name('name')<br/>class_name = walk parents<br/>fqn = package.class.name"]

    sym --> kind{kind?}
    kind -->|method/function/<br/>constructor| calls[walk body for<br/>method_invocation / call_expression]
    kind -->|class/impl/<br/>namespace| recurse[recurse into children]
    recurse --> walk

    calls --> callee["extract_callee per language:<br/>Java: object + name fields<br/>Py: function.attribute<br/>Go: selector_expression<br/>Rust: scoped_identifier<br/>C++: qualified_identifier"]
    callee --> edge["emit Edge<br/>from_symbol, callee_name,<br/>callee_class = imports[receiver]"]

    sym --> persist1[(INSERT code_symbols<br/>RETURNING id)]
    edge --> persist2[(INSERT code_edges<br/>to_symbol = NULL)]

    persist1 --> resolve
    persist2 --> resolve

    resolve["resolve_edges (2nd pass)<br/>UPDATE to_symbol = s.id<br/>WHERE s.name = e.callee_name<br/>AND s.class_name ≈ e.callee_class"]

    resolve --> done([1877 symbols<br/>11264 edges<br/>26% resolved → internal<br/>74% → external libs])
```

## 3. Graph Query Sequence (find_callers via recursive CTE)

```mermaid
sequenceDiagram
    actor User
    participant AL as agent_loop
    participant T as find_callers tool
    participant GQ as graph_query.py
    participant PG as Postgres

    User->>AL: "what calls validateCSR?"
    Note over AL: router structural regex → agent

    AL->>T: invoke({name:"validateCSR", depth:2})
    T->>GQ: find_callers("validateCSR", depth=2)

    GQ->>PG: WITH RECURSIVE callers AS (...)
    activate PG
    Note over PG: ANCHOR<br/>SELECT from_symbol WHERE<br/>callee_name='validateCSR'<br/>OR to_symbol IN targets<br/>→ depth 1: {CryptoServiceImpl.validateCSR,<br/>ClientOnboardingServiceImpl.*}
    Note over PG: RECURSIVE STEP<br/>JOIN callers ON to_symbol=sym_id<br/>depth<2, NOT in path<br/>→ depth 2: {...}
    Note over PG: TERMINATE (empty)
    PG-->>GQ: rows: [{depth, repo, filepath,<br/>class_name, name, start_line}]
    deactivate PG

    GQ-->>T: list[dict] sorted by depth
    T-->>AL: JSON [{depth, caller, file}]
    AL->>AL: append ToolMessage(<retrieved>...)
    AL-->>User: answer with [SOURCE_N] citations
```

## 4. Data Anatomy — Postgres Code Graph

```mermaid
erDiagram
    code_symbols ||--o{ code_edges : "from_symbol"
    code_symbols ||--o{ code_edges : "to_symbol (nullable)"

    code_symbols {
        serial id PK
        text repo
        text filepath
        text name "method/class name — INDEXED"
        text kind "method|function|constructor|class|interface|struct|enum|trait|impl|namespace"
        text class_name "enclosing container"
        text fqn "package.class.name"
        text language
        int start_line
        int end_line
    }

    code_edges {
        serial id PK
        int from_symbol FK "caller — always resolved"
        text relation "calls|imports|extends|implements"
        text callee_name "raw from AST — INDEXED"
        text callee_class "resolved via imports (best-effort)"
        int to_symbol FK "NULL = external library"
        int line
    }
```

**Why `to_symbol` is nullable:** `enrollment` calls `CryptoUtil.setCryptoServiceURL` from the external `com.example.crypto.api` JAR. The edge is recorded with `callee_name="setCryptoServiceURL"`, `callee_class="com.example.crypto.api.CryptoUtil"`, `to_symbol=NULL`. `find_callees` reports it as `kind=external` — so the agent knows it's a library call, not missing data.

**Resolution strategy:** name-only with import hints. ~26% of edges resolve to indexed symbols; the rest are JDK/Spring/external. Accuracy ~80-90% for resolved edges; false positives occur on common method names (`generate`, `get`) across repos.

## 5. Module Dependency Graph

```mermaid
flowchart TD
    cfg[config.py<br/>+ POSTGRES_DSN]

    subgraph chunking
        code[code.py<br/>get_parser, SYMBOL_TYPES,<br/>get_symbol_name FIXED]
    end

    subgraph indexing
        gdb[graph_db.py<br/>get_conn, setup_schema,<br/>reset_graph]
        cgr[code_graph.py<br/>extract_file, persist,<br/>resolve_edges,<br/>build_graph_for_repos]
        sql[graph_schema.sql]
    end
    cfg --> gdb
    sql -.read by.-> gdb
    gdb --> cgr
    code --> cgr

    subgraph retrieval
        gq[graph_query.py<br/>find_symbol, find_callers,<br/>find_callees, graph_stats]
    end
    gdb --> gq

    subgraph tools
        fs[find_symbol.py]
        fcr[find_callers.py]
        fce[find_callees.py]
    end
    gq --> fs
    gq --> fcr
    gq --> fce

    subgraph nodes
        rt[router.py<br/>+ STRUCTURAL_RE]
        al[agent_loop.py<br/>+ graph tools in prompt]
    end
    tools --> al

    idxg[scripts/index_graph.py]
    cgr --> idxg
    gq --> idxg

    smoke[scripts/smoke_test.py]
    gq --> smoke
    tools --> smoke

    style gdb fill:#ffe0b2,color:#000
    style cgr fill:#ffe0b2,color:#000
    style sql fill:#ffe0b2,color:#000
    style gq fill:#ffe0b2,color:#000
    style fs fill:#ffe0b2,color:#000
    style fcr fill:#ffe0b2,color:#000
    style fce fill:#ffe0b2,color:#000
    style idxg fill:#ffe0b2,color:#000
```

## 6. Phase 2 vs Phase 2.5 — What Changed

| Aspect | Phase 2 | Phase 2.5 |
|---|---|---|
| Retrieval modalities | 2 (semantic + keyword) | **3** (semantic + keyword + **structural**) |
| Postgres usage | checkpointer only | + `code_symbols`, `code_edges` (graph) |
| Agent tools | 4 (search_code, read_file, grep, web_search) | **7** (+ find_symbol, find_callers, find_callees) |
| "what calls X?" answer path | search_code + grep + read_file (3-6 iter) | `find_callers(X)` (1 tool call) |
| Router | history + LLM classifier | + `_STRUCTURAL_RE` regex short-circuit for graph queries |
| Languages w/ AST chunking | 5 (Java/Py/Go/JS/TS) | **7** (+ Rust, C/C++) |
| `_get_symbol_name` | first identifier-like child (returned Java return types — bug) | `child_by_field_name('name')` → correct method names |
| Indexing entry | `make index-v1` | `make index-v1` + `make index-graph` (or `make index-all`) |
| New deps | — | none (psycopg already present; reuses tree-sitter parsers) |
| Lines added | — | ~780 across 11 new files |

### Bugs found & fixed during Phase 2.5

| Bug | Fix |
|---|---|
| `psycopg` couldn't infer type of NULL `%(repo)s` param | Cast `%(repo)s::text` in CTE |
| Java method names extracted as return type (`Certificate` not `validateCSR`) | Use `child_by_field_name('name')` instead of first child match — also fixes Phase 1 chunk metadata |
| `resolve_edges()` reported total rows touched, not actually resolved | Count `WHERE to_symbol IS NOT NULL` after update |
| Router sent "what calls X?" to `simple_rag` (no tools) | Structural regex routes to `agent_loop` |

### Measured impact (acme-auth + acme-api)

| Metric | Value |
|---|---|
| Symbols indexed | 1,877 |
| Call edges | 11,264 |
| Resolution rate | 26.3% (rest = JDK/Spring/external libs) |
| `find_symbol('validateCSR')` | 3 defs (interface + impl + util) — instant |
| `find_callers('validateCSR', depth=3)` | 12 callers (3 prod, 9 test) — instant |
| Same question via Phase 2 agent | 6 iterations, ~15s |
