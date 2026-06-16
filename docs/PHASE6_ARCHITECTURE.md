# Phase 6 — Deployment, CLI, Auth

> Containerize the full stack for EC2; add a Claude-Code-style terminal
> client with a thin remote (SSE) mode; gate the API/UI behind
> bearer-key auth with an OIDC upgrade path. Runbook: `DEPLOY.md`.

---

## 1. System

```mermaid
flowchart LR
    subgraph laptop["Laptop"]
      CLI["cto (rich + prompt_toolkit)<br/>--remote → SSE client"]
      BROWSER["Browser"]
    end

    subgraph ec2["EC2 · docker compose"]
      direction TB
      OPROXY["oauth2-proxy :8080<br/>(--profile oidc)"]
      subgraph app["app :8000  (cto-app)"]
        direction TB
        AUTH["auth.py<br/>Bearer / X-Fwd-User"]
        API["FastAPI /query /sources /sessions"]
        UI["Gradio /ui"]
        GRAPH["LangGraph"]
        WATCH["watcher · confluence sched"]
      end
      Q[("Qdrant :6333")]
      PG[("Postgres :5432")]
      PX["Phoenix :6006"]
      subgraph mounts["bind mounts"]
        DATA[["./data → /app/data"]]
        HF[["./hf-cache → /hf-cache<br/>bge-reranker-v2-m3"]]
      end
    end

    CLI -- "Bearer CTO_API_KEY" --> AUTH
    BROWSER --> OPROXY -- "X-Forwarded-User" --> AUTH
    BROWSER -. "or direct (login form)" .-> UI
    AUTH --> API & UI
    API --> GRAPH
    GRAPH --> Q & PG
    GRAPH -. OTLP .-> PX
    WATCH --> DATA
    GRAPH --> HF

    classDef svc fill:#1f3a5f,color:#fff,stroke:#0b1d33
    classDef vol fill:#2d2d2d,color:#ddd,stroke:#555,stroke-dasharray:3
    classDef gate fill:#6b3fa0,color:#fff
    class Q,PG,PX svc
    class DATA,HF vol
    class OPROXY,AUTH gate
```

---

## 2. Request flow (remote CLI)

```mermaid
sequenceDiagram
    autonumber
    participant C as cto --remote
    participant A as auth.require_auth
    participant S as /query (SSE)
    participant G as LangGraph

    C->>A: POST /query  Authorization: Bearer …
    alt key invalid
      A-->>C: 401  WWW-Authenticate: Bearer
      Note right of C: ⎿ 401 — set CTO_API_KEY
    else key valid / proxy header
      A-->>S: principal (user_id)
      S->>G: stream(payload, updates)
      loop per node
        G-->>S: delta
        S-->>C: event:node {intent, scope, tool_calls…}
        Note right of C: ⏺ search(...)  ⎿ result preview
      end
      alt interrupt()
        S-->>C: event:interrupt {question, options}
        C->>S: POST /query {resume:"…"}
      end
      S-->>C: event:done {answer, citations, trace_url}
      Note right of C: ··· rule → Markdown answer + citations
    end
```

---

## 3. CLI internals

```mermaid
flowchart TD
    MAIN["main()<br/>argparse · CTO_REMOTE / CTO_API_KEY"]
    MAIN -->|"--remote"| RT["_run_turn_remote<br/>httpx.stream + _iter_sse<br/>(no torch/langgraph imports)"]
    MAIN -->|"local"| LT["_run_turn<br/>graph.stream(messages,updates)"]

    subgraph render["shared rendering"]
      SPIN["_Spinner.__rich__()<br/>self-animates @ 10 Hz<br/>note=node label"]
      BUL["_bullet ⏺ / _sub ⎿"]
      ANS["_answer_renderable<br/>Markdown + citations"]
    end
    RT --> SPIN & BUL & ANS
    LT --> SPIN & BUL & ANS

    PS["PromptSession<br/>FileHistory ~/.cto_history<br/>WordCompleter /…"]
    MAIN --> PS
    PS -->|"slash"| CMD["/new /sources /sessions /verbose /trace /clear /help"]
    CMD -->|"remote?"| HTTP["GET /sources, /sessions<br/>+ Bearer header"]

    classDef thin fill:#0d4f3c,color:#fff
    classDef heavy fill:#5a3a00,color:#fff
    class RT,HTTP thin
    class LT heavy
```

---

## 4. Auth decision tree

```mermaid
flowchart TD
    R["require_auth(request, cred)"] --> T{"CTO_TRUST_FORWARDED_USER?"}
    T -->|"yes + X-Fwd-User present"| U["return header → trusted user_id"]
    T -->|"no / header absent"| K{"CTO_API_KEYS set?"}
    K -->|"yes"| C{"Bearer matches?<br/>sha256 + hmac.compare_digest"}
    C -->|"yes"| OK["return 'api-key'"]
    C -->|"no"| E401["raise 401"]
    K -->|"no"| T2{"trust-fwd on but no header?"}
    T2 -->|"yes"| E401
    T2 -->|"no"| OPEN["return None  (auth disabled — dev)"]

    classDef bad fill:#7a1f1f,color:#fff
    classDef good fill:#0d4f3c,color:#fff
    class E401 bad
    class U,OK,OPEN good
```

`principal` flows into `/query`: when it's a real user (proxy header)
it **overrides** `req.user_id`, so Phoenix `user.id` and the `Store`
namespace become trustworthy.

---

## 5. vs Phase 5

| | Phase 5 | Phase 6 |
|---|---|---|
| Run surface | `make serve` on host | `docker compose`: qdrant + postgres + phoenix + **app** (+ optional oauth2-proxy) |
| Reranker | host venv site-packages | bind-mounted `./hf-cache` (`make prewarm-reranker`) |
| CLI | `scripts/ask.py` (one-shot) | **`cto`** REPL: ⏺/⎿ bullets, self-animating spinner, slash cmds, clarify panel; `--remote` thin SSE mode |
| `/query` SSE | `node`/`done` minimal | + `intent` `repo_scope` `resolved_query` `eval` `refine_hint` `trace_url` |
| Auth | none | bearer keys (`CTO_API_KEYS`), Gradio login (`CTO_UI_USER/PASSWORD`), `X-Forwarded-User` trust, oauth2-proxy compose profile |
| Env | scattered | `.env.example` is exhaustive (33 vars, `[file:line]` refs) |
| Make | `infra` `serve` `ask` | + `chat` `chat-remote` `docker-{build,up,down,logs,shell,exec,restart}` `prewarm-reranker` `gen-api-key` |
| Docs | PHASE5_ARCHITECTURE | + DEPLOY.md (EC2 runbook §1–10), this file |
| Deferred | — | Go remote-CLI (lipgloss/glamour); `PHOENIX_PUBLIC_HOST`; per-user repo ACLs in retrieval |
