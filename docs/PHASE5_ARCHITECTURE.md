# Phase 5 — Architecture & Code Flow

> Multi-source expansion + tiered retrieval. Adds three new source surfaces — **Confluence** (auto-synced wiki), **git history** (live from `.git`), **JIRA** (live REST lookup) — behind a **tiered retrieval cascade** (code → local docs → Confluence) that early-exits at the first strong tier. Plus guard/routing fixes so general-knowledge answers survive (relatedness bands), doc-intent queries reach the docs, and the cross-source join (ticket → commits → wiki → code) works. Repo reorg: tests → `tests/`, full-index pipeline → a module, Phase-0 baseline → `scripts/legacy/`.
>
> Diverges from the original Phase-5 plan: built **Confluence DC** (not GitLab) + **on-demand git/JIRA tools** (not webhook workers) + the **cascade** (not in the plan). MCP / Slack / GitLab webhooks remain deferred.

## 1. System Architecture

```mermaid
flowchart LR
    subgraph Sources
        repos[(data/repos/<br/>git working trees)]
        docs[(data/docs/<br/>PDF · md)]
        conf[(Confluence DC<br/>spaces)]
        jiraSrc[(JIRA DC)]
    end

    subgraph Ingest["Ingestion"]
        watch[watcher<br/>fs events → incremental]
        full[full_index<br/>make index-v1]
        csched["confluence scheduler<br/>24h version-diff"]:::new
    end

    subgraph Stores
        qcode[(Qdrant 'code'<br/>dense+BM25)]
        qdocs[(Qdrant 'docs'<br/>local + confluence)]:::new
        pg[(Postgres<br/>code graph · checkpoints ·<br/>store · confluence_pages)]:::new
    end

    subgraph Retrieval
        casc["cascade_search<br/>code → docs → confluence<br/>early-exit ≥2 strong"]:::new
    end

    subgraph Tools["Agent tools (18)"]
        t_search[search → cascade]:::new
        t_code[search_code / search_docs]
        t_graph[find_callers / callees / symbol]
        t_git["git_log · git_show · git_blame<br/>find_commits_for_jira"]:::new
        t_jira["jira_lookup · jira_search"]:::new
        t_exec[execute_code · grep · read_file · web]
    end

    repos --> watch --> qcode & pg
    repos --> full --> qcode
    conf --> csched --> qdocs
    docs --> qdocs
    casc --> qcode & qdocs
    t_search --> casc
    t_git -. live .-> repos
    t_jira -. live REST .-> jiraSrc

    classDef new fill:#d6f0d6,color:#000,stroke:#2a8,stroke-width:2px
```

## 2. Retrieval Cascade

```mermaid
flowchart TD
    q[query + scope] --> t1
    t1["Tier 1: CODE<br/>hybrid_search code, repo-filtered<br/>→ rerank top-8"]
    t1 --> c1{≥2 chunks<br/>≥0.30?}
    c1 -->|yes| done[merge-rerank pool → top-k]:::stop
    c1 -->|no| t2
    t2["Tier 2: LOCAL DOCS<br/>docs coll, source≠confluence"]
    t2 --> c2{≥2 strong?}
    c2 -->|yes| done
    c2 -->|no| t3
    t3["Tier 3: CONFLUENCE<br/>docs coll, source=confluence<br/>relates_to filter, fallback unscoped"]
    t3 --> done

    note["carries best-so-far forward;<br/>final = merge-rerank of ALL visited tiers"]:::note
    done -.-> note

    classDef stop fill:#d6f0d6,color:#000,stroke:#2a8
    classDef note fill:#fff,color:#555,stroke:#bbb,stroke-dasharray:3
```

`simple_rag` uses it directly; the `search` tool exposes it (agent's default first call). JIRA/git are **not** tiers — they're on-demand API tools, no embeddings (same rationale as their no-index design). Future tiers (JIRA-indexed, logs) slot into `_TIERS`.

## 3. Confluence Connector — version-diff sync

```mermaid
sequenceDiagram
    autonumber
    participant S as Scheduler (24h)
    participant C as ConfluenceClient
    participant P as Postgres
    participant Q as Qdrant 'docs'

    S->>P: due? (now - last_run ≥ 24h)
    S->>C: list_pages(space) → {id: version}  (paginated CQL/content)
    S->>P: SELECT page_id, version (confluence_pages)
    Note over S: diff → changed = remote.v > local.v · deleted = local − remote
    loop changed pages
        S->>C: get_page(id) → body.storage + ancestors
        Note over S: storage_to_markdown (ac:* macros) → chunk_markdown_file
        S->>Q: delete(source=confluence, page_id) → upsert chunks
        S->>P: UPSERT confluence_pages (version, title)
    end
    loop deleted pages
        S->>Q: delete chunks
        S->>P: DELETE confluence_pages row
    end
    S->>P: record_sync(last_run, counts)
```

Idempotent on `uuid5(confluence|space|page_id|chunk_idx)`. Chunks land in the existing `docs` collection with `source=confluence`, `url`, `breadcrumb`, `relates_to` (code-symbol linkage). `make confluence-{sync,status,reset}`; scheduler auto-starts in `make serve` lifespan.

## 4. The Cross-Source Join (the differentiator)

```mermaid
flowchart LR
    Q["'what's the story on PROJ-69412?'"] --> A[agent_loop]
    A --> J["jira_lookup(PROJ-69412)<br/>→ summary, status, why"]:::new
    A --> G["find_commits_for_jira(PROJ-69412)<br/>→ commits across ALL repos"]:::new
    A --> D["search_docs('PROJ-69412')<br/>→ Confluence design note"]
    J & G & D --> SYN[synthesized cited answer]:::stop

    classDef new fill:#d6f0d6,color:#000,stroke:#2a8
    classDef stop fill:#ffe9b3,color:#000,stroke:#cc8400
```

No single source answers "why was this built, what code changed, where's it documented" — the agent joins three live/indexed sources into one cited answer.

## 5. Module Dependencies (Phase 5 additions)

```mermaid
flowchart TD
    casc[retrieval/cascade.py]:::new --> hyb[retrieval/hybrid.py]
    casc --> rer[retrieval/reranker.py]
    casc --> docs[ingest/docs.py]
    simple[nodes/simple_rag.py] --> casc
    searchT[tools/search.py]:::new --> casc

    conf[connectors/confluence.py]:::new --> smd[connectors/storage_md.py]:::new
    conf --> docs
    conf --> graphdb[indexing/graph_db.py]
    sched[connectors/scheduler.py]:::new --> conf
    app[api/app.py] --> sched

    gitT[tools/git_history.py]:::new --> repo[tools/_repo.py]
    jiraT[tools/jira.py]:::new --> cfg[config.py]
    fullidx[ingest/full_index.py]:::new --> chunk[chunking] & embed[embed.py]

    classDef new fill:#d6f0d6,color:#000,stroke:#2a8,stroke-width:2px
```

## 6. Decision Logic (Phase 5 additions)

| Gate | Condition | Outcome |
|---|---|---|
| router `_DOCS_RE` | runbook/confluence/docs/spec phrasing | → agent (has search_docs) |
| clarify docs-intent skip | `_DOCS_RE` matches | skip repo-disambiguation (docs aren't repo-keyed) |
| cascade early-exit | tier yields ≥ `MIN_STRONG=2` chunks ≥ 0.30 | stop, don't descend |
| hybrid docs repo-filter | `collection==docs` | match `repo` OR `relates_to` (should-clause) |
| output_guard abstain | weak retrieval ∧ answer **cites** chunks | ABSTAIN (confabulation) |
| output_guard band | weak ∧ uncited, score ≥ 0.12 | keep + "domain-adjacent" note |
| output_guard band | weak ∧ uncited, score < 0.12 | keep + "off-domain" note |
| evaluator empty-answer | "nothing found" ∧ <2 chunks | retry with cascade/search_docs hint (no LLM grade) |
| confluence sync | `remote.version > stored.version` | re-index that page only |

## 7. Phase 4.6 → 5 Comparison

| Aspect | 4.6 | 5 |
|---|---|---|
| Sources | code, local docs | + **Confluence**, **git history**, **JIRA** |
| Retrieval | per-collection hybrid+rerank | + **cascade** (code→docs→confluence, early-exit) |
| Tools | 11 | **18** (+search, +4 git, +2 jira) |
| Doc reachability | search_docs (agent only) | simple_rag also via cascade; `_DOCS_RE` routes doc-intent to agent |
| General-knowledge Q | abstain (stream-then-erase bug) | citation-aware abstain + relatedness bands; answer survives |
| Confluence repo filter | n/a | `relates_to` should-clause (pages aren't repo-keyed) |
| Cross-source join | — | ticket → commits → wiki → code in one answer |
| Repo layout | tests + pipeline in scripts/ | tests/ · `ingest/full_index` module · `scripts/legacy/` |
| Tests | 39 + 27 | **39 + 29 + 29** (+ test_confluence) |

## 8. Files (Phase 5)

| File | Role |
|---|---|
| `connectors/confluence.py` | client (DC PAT/Bearer) · version-diff sync · delete_space · sync_status |
| `connectors/storage_md.py` | storage-format XML → markdown (ac:* macros, tables, links, tasks) |
| `connectors/scheduler.py` | 24h per-space daemon, gated on last_run |
| `retrieval/cascade.py` | `cascade_search` tiers + early-exit |
| `tools/search.py` | cascade-backed general search (agent's first call) |
| `tools/git_history.py` | git_log · git_show · git_blame · find_commits_for_jira |
| `tools/jira.py` | jira_lookup · jira_search (live REST, no-op unconfigured) |
| `ingest/full_index.py` | full-rebuild pipeline (extracted from scripts) |
| `scripts/confluence_sync.py` | CLI: --space/--force/--status/--reset |
| `tests/` | all suites + conftest.py |
| `scripts/legacy/` | Phase-0 baseline (index_repos, query) |

## 9. Known Limitations / Follow-ups

- **Relatedness bands are heuristic** (0.30 / 0.12 thresholds, uncalibrated) — planned replacement: LLM `topic` field in query_analysis (approach C, noted in memory).
- **JIRA/git not in cascade** — on-demand only; index JIRA → cascade tier if semantic "tickets about X" is needed.
- **Confluence version-diff misses comment-only edits** (no body version bump).
- **full_index vs incremental** still two code paths to "chunk→embed→upsert" — candidate to unify.
- **MCP / Slack / GitLab webhooks** — original Phase-5 plan items, deferred.
