# Phase 1 — Architecture & Code Flow

> Renders in GitLab, GitHub, VS Code (Markdown Preview Mermaid Support extension).

## 1. System Architecture

```mermaid
flowchart LR
    subgraph Sources
        repos[(data/repos/<br/>acme-auth<br/>acme-api)]
    end

    subgraph Indexing["Indexing Pipeline (offline)"]
        idx[index_repos_v1.py]
        svc[service_metadata.py]
        reg[chunking/registry.py]
        ts[code.py<br/>tree-sitter AST]
        md[markdown.py<br/>header hierarchy]
        fb[fallback.py<br/>token window]
        vocab[hybrid.py<br/>build_vocab]
        emb[embed.py]
    end

    subgraph External
        litellm[LiteLLM Gateway<br/>corp LiteLLM proxy]
        embModel[[text-embedding-3-large]]
        claude[[claude-sonnet-4-6]]
    end

    subgraph Storage
        qdrant[(Qdrant<br/>collection: code<br/>dense + sparse)]
        vfile[(data/vocab.json)]
    end

    subgraph Query["Query Pipeline (online)"]
        q[query_v1.py]
        hyb[retrieve_hybrid<br/>dense+sparse+RRF]
        rr[reranker.py<br/>bge-reranker-v2-m3<br/>local CrossEncoder]
        gen[generate_answer]
    end

    repos --> idx
    idx --> svc
    idx --> reg
    reg --> ts
    reg --> md
    reg --> fb
    idx --> vocab
    idx --> emb
    emb --> litellm
    litellm --> embModel
    vocab --> vfile
    idx --> qdrant

    q --> hyb
    hyb --> emb
    hyb --> vfile
    hyb --> qdrant
    hyb --> rr
    rr --> gen
    gen --> litellm
    litellm --> claude
```

## 2. Indexing Flow (per repo)

```mermaid
flowchart TD
    start([data/repos/&lt;repo&gt;]) --> meta[extract_repo_metadata<br/>scan application.properties + *.java]
    meta --> metaOut{{service_name<br/>depends_on<br/>exposed_endpoints}}

    metaOut --> walk[walk all files<br/>filter by CODE_EXTENSIONS<br/>skip vendor/.git/build]

    walk --> route{registry.chunk_file<br/>by extension}
    route -->|.java .py .go .js .ts| ast[code.py<br/>tree-sitter parse → AST<br/>collect method/class nodes<br/>1 symbol = 1 chunk<br/>split if &gt;1500 tokens]
    route -->|.md| mdc[markdown.py<br/>split on headings<br/>maintain breadcrumb stack]
    route -->|other| fbc[fallback.py<br/>500-token windows]

    ast --> enrich
    mdc --> enrich
    fbc --> enrich

    enrich[enrich each chunk:<br/>+ service_name, depends_on, layer, calls_services → payload<br/>+ '// Service: X | Layer: Y | Calls: Z' → text prefix]

    enrich --> collect[(all_chunks)]

    collect --> bv[build_vocab_from_chunks<br/>tokenize all → term→id map]
    bv --> savev[(vocab.json)]

    collect --> create[Qdrant: delete + create collection<br/>dense: 1024-dim cosine<br/>sparse: inverted index]

    create --> batch[batch of 64 chunks]
    batch --> dense[embed_texts → LiteLLM<br/>→ 1024 floats each]
    batch --> sparse[compute_sparse_vector<br/>term freq → indices+values]
    dense --> upsert
    sparse --> upsert
    upsert[qdrant.upsert<br/>PointStruct: id, dense, sparse, payload]
    upsert --> done([798 points indexed])
```

## 3. Query Flow

```mermaid
sequenceDiagram
    actor User
    participant Q as query_v1.py
    participant E as embed.py
    participant L as LiteLLM Gateway
    participant V as vocab.json
    participant DB as Qdrant
    participant R as bge-reranker<br/>(local, in-process)

    User->>Q: "what calls the crypto service?"
    Q->>V: load_vocab()
    V-->>Q: {term: id, ...} 6779 terms

    rect rgba(80, 140, 255, 0.12)
    note over Q,DB: Hybrid Retrieve (top-30)
    Q->>E: embed_query(question)
    E->>L: POST /embeddings<br/>model=text-embedding-3-large
    L-->>E: [1024 floats]
    E-->>Q: dense_vector
    Q->>Q: tokenize query → lookup vocab<br/>→ sparse_vector
    Q->>DB: query_points(<br/>  prefetch=[dense@30, sparse@30],<br/>  fusion=RRF, limit=30)
    DB-->>Q: 30 candidates (fused)
    end

    rect rgba(255, 180, 60, 0.12)
    note over Q,R: Rerank (30 → 8)
    Q->>R: rerank(query, 30 chunks, top_k=8)
    R->>R: CrossEncoder.predict<br/>30 × (query, chunk) pairs
    R-->>Q: top-8 by rerank_score
    end

    rect rgba(80, 200, 120, 0.12)
    note over Q,L: Generate
    Q->>Q: format: [1] (repo/path:line) text...
    Q->>L: POST /chat/completions<br/>model=claude-sonnet-4-6<br/>system: "cite [N], context-only"
    L-->>Q: answer with citations
    end

    Q-->>User: answer + sources
```

## 4. Chunk Anatomy

```mermaid
flowchart LR
    subgraph chunk["One Qdrant Point"]
        direction TB
        id["id: 142"]
        subgraph vectors
            dv["dense: [0.023, -0.118, ...]<br/>1024 floats<br/>from text-embedding-3-large"]
            sv["sparse: {indices:[42,891,...],<br/>values:[3.0,1.0,...]}<br/>term-frequency"]
        end
        subgraph payload
            txt["text:<br/>// Service: enrollment-service | Layer: service-impl | Calls: crypto-service<br/>// File: .../CryptoServiceImpl.java | method_declaration: validateCSR | Class: CryptoServiceImpl<br/>public Certificate validateCSR(...) {...}"]
            m["repo: acme-auth<br/>filepath: src/.../CryptoServiceImpl.java<br/>start_line: 18, end_line: 21<br/>symbol_name: validateCSR<br/>class_name: CryptoServiceImpl<br/>symbol_type: method_declaration<br/>language: java<br/>chunk_type: ast_symbol<br/>service_name: enrollment-service<br/>depends_on: [crypto-service, ratelimit-service]<br/>calls_services: [crypto-service]<br/>layer: service-impl"]
        end
    end
```

## 5. Module Dependency Graph

```mermaid
flowchart TD
    cfg[config.py<br/>LITELLM_URL, MODELS, QDRANT_URL]

    emb[embed.py<br/>embed_texts / embed_query]
    cfg --> emb

    subgraph chunking
        reg[registry.py]
        code[code.py]
        mkd[markdown.py]
        fb[fallback.py]
        sm[service_metadata.py]
        reg --> code
        reg --> mkd
        reg --> fb
    end

    subgraph retrieval
        hyb[hybrid.py]
        rr[reranker.py]
    end
    cfg --> hyb
    emb --> hyb

    idx[scripts/index_repos_v1.py]
    qry[scripts/query_v1.py]
    evl[eval/run_eval_v1.py]

    cfg --> idx
    emb --> idx
    reg --> idx
    sm --> idx
    hyb -->|build_vocab,<br/>compute_sparse| idx

    cfg --> qry
    emb --> qry
    hyb -->|_tokenize_for_sparse| qry
    rr --> qry

    cfg --> evl
    emb --> evl
    hyb -->|_tokenize_for_sparse| evl
    rr --> evl

    style idx fill:#e1f5e1,color:#000
    style qry fill:#e1e5f5,color:#000
    style evl fill:#f5e1e1,color:#000
```

## 6. Phase 0 vs Phase 1 — What Changed

| Stage | Phase 0 | Phase 1 |
|---|---|---|
| Chunking | 500-token sliding window | tree-sitter AST (code) / header-hierarchy (md) / fallback |
| Enrichment | none | `// Service | Layer | Calls | File | Symbol | Class` prefix |
| Metadata | repo, filepath, start_line | + symbol_name, class_name, language, service_name, depends_on, calls_services, layer |
| Vectors | dense only | dense + sparse (term-frequency) |
| Search | cosine top-5 | dense + sparse → RRF fusion → top-30 |
| Post-process | none | bge-reranker-v2-m3 cross-encoder → top-8 |
| Recall@1 | 0.60 | **1.00** |
| Recall@5 | 0.80 | **1.00** |
