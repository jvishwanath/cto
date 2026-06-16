# Phase 0 — Architecture & Code Flow

> Baseline RAG: naive chunking, dense-only vectors, no reranker. Establishes the eval baseline every later phase must beat.

## 1. System Architecture

```mermaid
flowchart LR
    subgraph Sources
        repos[(data/repos/<br/>acme-auth<br/>acme-api)]
    end

    subgraph Indexing["Indexing (offline)"]
        idx[scripts/index_repos.py]
        chunk[chunk_text<br/>500-token windows<br/>50-token overlap]
        emb[embed.py]
    end

    subgraph External
        litellm[LiteLLM Gateway<br/>corp LiteLLM proxy]
        embModel[[text-embedding-3-large]]
        claude[[claude-sonnet-4-6]]
    end

    subgraph Storage
        qdrant[(Qdrant<br/>collection: code<br/>dense only, cosine)]
    end

    subgraph Query["Query (online)"]
        q[scripts/query.py]
        ret[retrieve<br/>cosine top-5]
        gen[generate_answer]
    end

    subgraph Eval
        ev[eval/run_eval.py<br/>recall@k, keyword recall]
        gs[(golden_set.jsonl)]
    end

    repos --> idx
    idx --> chunk
    chunk --> emb
    emb --> litellm
    litellm --> embModel
    idx --> qdrant

    q --> ret
    ret --> emb
    ret --> qdrant
    ret --> gen
    gen --> litellm
    litellm --> claude

    gs --> ev
    ev --> emb
    ev --> qdrant
```

## 2. Indexing Flow

```mermaid
flowchart TD
    start([data/repos/]) --> walk[rglob all files<br/>filter CODE_EXTENSIONS<br/>skip .git, vendor, node_modules]

    walk --> read[read file as UTF-8]
    read --> tok[tiktoken.encode<br/>cl100k_base]

    tok --> slice[slide 500-token window<br/>step = 450 → 50-token overlap]
    slice --> dec[decode each window → text<br/>compute start_line]
    dec --> meta["chunk = {text, metadata:<br/>{repo, filepath, start_line,<br/>chunk_type: naive_window}}"]

    meta --> collect[(903 chunks)]

    collect --> create[Qdrant: delete + create<br/>VectorParams: 1024-dim, COSINE]

    create --> batch[batch of 64 chunks]
    batch --> embed[embed_texts → LiteLLM<br/>text-embedding-3-large<br/>dimensions=1024]
    embed --> upsert[qdrant.upsert<br/>PointStruct: id, vector, payload]
    upsert --> done([903 points, dense only])
```

## 3. Query Flow

```mermaid
sequenceDiagram
    actor User
    participant Q as query.py
    participant E as embed.py
    participant L as LiteLLM Gateway
    participant DB as Qdrant

    User->>Q: "how does JWT validation work?"

    rect rgba(80, 140, 255, 0.12)
    note over Q,DB: Retrieve (top-5)
    Q->>E: embed_query(question)
    E->>L: POST /embeddings<br/>model=text-embedding-3-large<br/>dimensions=1024
    L-->>E: [1024 floats]
    E-->>Q: query_vector
    Q->>DB: query_points(vector, limit=5)
    DB-->>Q: 5 nearest chunks<br/>(HNSW + cosine)
    end

    rect rgba(80, 200, 120, 0.12)
    note over Q,L: Generate
    Q->>Q: format: [1] (repo/path:line, score)<br/>chunk text...
    Q->>L: POST /chat/completions<br/>model=claude-sonnet-4-6<br/>system: "answer from context, cite [N]"
    L-->>Q: answer
    end

    Q-->>User: answer + sources
```

## 4. Chunk Anatomy

```mermaid
flowchart LR
    subgraph chunk["One Qdrant Point (Phase 0)"]
        direction TB
        id["id: 142"]
        subgraph vector
            dv["vector: [0.023, -0.118, ..., 0.089]<br/>1024 floats<br/>cosine distance"]
        end
        subgraph payload
            txt["text:<br/>(raw 500-token slice — may cut mid-method)<br/>'...validateToken(token);<br/>  if (claims == null) {<br/>    throw new...'"]
            m["repo: acme-auth<br/>filepath: src/.../JwtValidator.java<br/>start_line: 47<br/>chunk_type: naive_window"]
        end
    end
```

**No** sparse vector. **No** symbol/class/service metadata. **No** context prefix. Just raw text slices.

## 5. Module Dependency Graph

```mermaid
flowchart TD
    cfg[config.py<br/>LITELLM_URL, LITELLM_API_KEY,<br/>QDRANT_URL, MODELS]

    emb[embed.py<br/>OpenAI client → LiteLLM<br/>embed_texts / embed_query]
    cfg --> emb

    idx[scripts/index_repos.py<br/>walk + chunk + embed + upsert]
    qry[scripts/query.py<br/>embed + search + generate]
    evl[eval/run_eval.py<br/>embed + search + score]

    cfg --> idx
    emb --> idx

    cfg --> qry
    emb --> qry

    cfg --> evl
    emb --> evl

    style idx fill:#e1f5e1,color:#000
    style qry fill:#e1e5f5,color:#000
    style evl fill:#f5e1e1,color:#000
```

5 files total. No `chunking/` or `retrieval/` packages — all logic inline in scripts.

## 6. Baseline Results

| K | Recall@K | Repo Hit | Keyword Recall |
|---|---|---|---|
| 1 | 0.600 | 0.800 | 0.767 |
| 3 | 0.800 | 1.000 | 0.767 |
| 5 | 0.800 | 1.000 | 0.767 |
| 10 | 1.000 | 1.000 | 0.933 |

**Failure case:** "What endpoint handles device enrollment?" → ✗ at k=5 (KW recall 0.33). Naive 500-token windows split the `@PostMapping` annotation from its method body — neither chunk fully answers the query. This is the motivating example for Phase 1's tree-sitter chunking.

## 7. What Phase 0 Establishes vs Has Nothing

| Aspect | Phase 0 |
|---|---|
| Chunking | Naive 500-token sliding window, 50-token overlap |
| Vectors | Dense only (1024-dim, cosine) |
| Search | Single cosine kNN, top-5 |
| Reranking | None |
| Metadata | repo, filepath, start_line only |
| Enrichment | None |
| Eval | recall@k + keyword recall against 5-question golden set |
| Files | 5 (config, embed, index_repos, query, run_eval) |
| Chunks indexed | 903 |
