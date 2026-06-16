# Phase 3 — Architecture & Code Flow

> CodeExecutor subgraph + Docker sandbox. Adds the `execute_code` tool: agent can write & run Python/bash in an isolated container (no network, read-only FS, repo mounted `:ro`, resource caps) with a write→run→fix retry loop. First multi-agent pattern: supervisor (orchestrator) delegates to worker (executor subgraph).

## 1. System Architecture

```mermaid
flowchart LR
    subgraph Graph["Main StateGraph (Phase 2)"]
        router[router]
        srag[simple_rag]
        aloop[agent_loop]
        resp[respond]
        router --> srag
        router --> aloop
        srag --> resp
        aloop --> resp
    end

    subgraph Tools["Agent Tools (8)"]
        sc[search_code]
        rf[read_file]
        gp[grep]
        fs[find_symbol]
        fcr[find_callers]
        fce[find_callees]
        ws[web_search]
        ec[execute_code]:::new
    end

    subgraph Executor["CodeExecutor SUBGRAPH"]
        direction TB
        wc[write_code]:::new
        rs[run_sandbox]:::new
        fc[fix_code]:::new
        chk{check_result}:::new
        wc --> rs
        rs --> chk
        chk -->|exit≠0 &<br/>attempt<4| fc
        chk -->|exit=0 or<br/>attempt≥4| done([END])
        fc --> rs
    end

    subgraph Sandbox["DockerSandbox"]
        dk[docker run --rm<br/>--network=none<br/>--read-only<br/>--tmpfs /tmp<br/>--memory 512m --cpus 1<br/>--pids-limit 128<br/>--cap-drop=ALL<br/>--security-opt no-new-privileges<br/>--user nobody<br/>-v repo:/repo:ro]:::new
    end

    subgraph Storage
        repos[(data/repos/)]
        qdrant[(Qdrant)]
        pg[(Postgres)]
    end

    aloop --> sc
    aloop --> rf
    aloop --> gp
    aloop --> fs
    aloop --> fcr
    aloop --> fce
    aloop --> ws
    aloop --> ec

    ec --> Executor
    rs --> dk
    dk --> repos

    sc --> qdrant
    fs --> pg
    fcr --> pg
    fce --> pg
    rf --> repos
    gp --> repos
    Graph -.checkpointer.-> pg

    litellm[LiteLLM Gateway<br/>+ gateway guardrails]
    aloop --> litellm
    wc --> litellm
    fc --> litellm

    classDef new fill:#ffe0b2,color:#000,stroke:#e65100
```

## 2. CodeExecutor Subgraph Flow

```mermaid
flowchart TD
    start([execute_code tool invoked<br/>task, repo?, lang]) --> docker{Docker<br/>available?}
    docker -->|no| degrade["return 'Error: Docker not<br/>available — use read_file instead'"]
    docker -->|yes| invoke[executor.invoke<br/>task, repo, lang, attempt=0]

    invoke --> wc[write_code<br/>LLM with safety system prompt:<br/>'no network, /repo read-only,<br/>output one fenced block'<br/>→ extract code, attempt=1]

    wc --> rs[run_sandbox<br/>DockerSandbox.run<br/>→ stdout, stderr,<br/>exit_code, timed_out,<br/>duration_ms]

    rs --> chk{check_result}
    chk -->|exit=0 &<br/>!timed_out| done
    chk -->|attempt ≥ 4| done
    chk -->|else| fc[fix_code<br/>LLM sees: task + prev code<br/>+ stderr + stdout<br/>→ corrected code,<br/>attempt += 1]
    fc --> rs

    done([return JSON:<br/>exit_code, attempts,<br/>stdout, stderr,<br/>code_executed])

    done --> wrap["agent_loop wraps in<br/>&lt;sandbox_output&gt;...&lt;/sandbox_output&gt;<br/>appends as ToolMessage"]

    style wc fill:#ffe0b2,color:#000
    style rs fill:#ffe0b2,color:#000
    style fc fill:#ffe0b2,color:#000
    style chk fill:#ffe0b2,color:#000
```

## 3. Sandbox Run Sequence

```mermaid
sequenceDiagram
    participant AL as agent_loop
    participant EC as execute_code tool
    participant EX as CodeExecutor<br/>subgraph
    participant L as LiteLLM<br/>(claude-sonnet-4-6)
    participant SB as DockerSandbox
    participant D as Docker daemon

    AL->>EC: invoke({task, repo})
    EC->>SB: available()?
    SB-->>EC: True
    EC->>EX: invoke({task, repo, lang, attempt:0})

    rect rgba(255, 180, 60, 0.12)
    note over EX,L: write_code
    EX->>L: [System: sandbox constraints]<br/>[Human: Task + repo hint]
    L-->>EX: python code block
    EX->>EX: extract code, attempt=1
    end

    loop until exit=0 or attempt=4
        rect rgba(80, 140, 255, 0.12)
        note over EX,D: run_sandbox
        EX->>SB: run(code, lang, repo, timeout=30)
        SB->>SB: write code → tempdir/main.py
        SB->>D: docker run --rm --network=none<br/>--read-only --tmpfs /tmp ...<br/>-v tempdir:/work:ro -v repo:/repo:ro<br/>image timeout 30s python /work/main.py
        D-->>SB: stdout, stderr, exit_code
        SB->>SB: sanitize_output<br/>(ANSI strip, binary detect, truncate 4KB)
        SB-->>EX: SandboxResult
        end

        alt exit_code != 0 and attempt < 4
            rect rgba(255, 80, 80, 0.12)
            note over EX,L: fix_code
            EX->>L: [System: fix prompt]<br/>[Human: task + prev code + stderr]
            L-->>EX: corrected code
            EX->>EX: attempt += 1
            end
        end
    end

    EX-->>EC: {exit_code, stdout, stderr,<br/>code, attempt, duration_ms}
    EC-->>AL: JSON string
    AL->>AL: ToolMessage(<br/>"<sandbox_output>...</sandbox_output>")
```

## 3b. End-to-End: `make ask` → `execute_code` → sandbox

```mermaid
flowchart TD
    user["make ask Q='verify the regex in<br/>validateEmail matches foo@bar' S=exec1"]
    user --> ask[scripts/ask.py]
    ask --> getapp["get_app() → compiled main StateGraph<br/>+ PostgresSaver checkpointer"]
    getapp --> stream["app.stream({query, messages:[Human(q)]},<br/>thread_id='exec1')"]

    stream --> ckpt1[(load checkpoint<br/>thread_id=exec1)]
    ckpt1 --> START1([START])

    START1 --> R["router node<br/>history? structural regex?<br/>→ LLM classify"]
    R --> rdec{"route?"}
    rdec -->|simple| SR[simple_rag<br/>not taken here]
    rdec -->|agent| AL

    subgraph AL["agent_loop node (hand-rolled ReAct)"]
        direction TB
        al0["msgs = [System] + history + Human(q)"]
        al0 --> al1["llm.bind_tools(ALL_TOOLS).invoke(msgs)"]
        al1 --> al2{tool_calls?}
        al2 -->|no| alFinal["final answer → break"]
        al2 -->|yes| al3["for each tool_call:"]

        al3 --> tc1["iter 1: search_code<br/>find the regex"]
        al3 --> tc2["iter 2: read_file<br/>get exact pattern"]
        al3 --> tc3["iter 3: execute_code<br/>{task:'test regex vs foo@bar',<br/>repo:'acme-auth'}"]:::exec

        tc1 --> wrap1["ToolMessage(&lt;retrieved&gt;)"]
        tc2 --> wrap2["ToolMessage(&lt;retrieved&gt;)"]
        wrap1 --> al1
        wrap2 --> al1
    end

    tc3 --> EC

    subgraph EC["execute_code tool"]
        direction TB
        ec0{Docker<br/>available?}
        ec0 -->|no| ecErr["'Error: Docker not available'"]
        ec0 -->|yes| ecInv["get_executor().invoke(...)"]
    end

    ecInv --> EX

    subgraph EX["CodeExecutor SUBGRAPH"]
        direction TB
        exS([START]) --> WC["write_code<br/>LLM w/ safety prompt<br/>→ code, attempt=1"]
        WC --> RS["run_sandbox<br/>DockerSandbox.run()"]
        RS --> DK

        subgraph DK["DockerSandbox"]
            direction TB
            dk0["tmpdir/main.py"]
            dk0 --> dk1["docker run --rm<br/>--network=none --read-only<br/>--tmpfs /tmp --memory 512m<br/>--cpus 1 --pids-limit=128<br/>--cap-drop=ALL<br/>--security-opt=no-new-privileges<br/>--user nobody<br/>-v tmpdir:/work:ro<br/>-v repo:/repo:ro<br/>image timeout 30s python main.py"]
            dk1 --> dk2["sanitize_output<br/>(ANSI/binary/4KB)"]
            dk2 --> dk3["SandboxResult"]
        end

        DK --> CHK{"check_result"}
        CHK -->|done| exE
        CHK -->|retry| FC["fix_code<br/>LLM: task+code+stderr<br/>→ corrected, attempt++"]
        FC --> RS
        exE([END])
    end

    exE --> ecRet["JSON: exit_code, attempts,<br/>stdout, stderr, code_executed"]
    ecErr --> ecRet
    ecRet --> wrap3["ToolMessage(<br/>&lt;sandbox_output&gt;{json}&lt;/sandbox_output&gt;)"]:::exec

    wrap3 --> al1
    alFinal --> alRet["return delta:<br/>{messages, answer,<br/>retrieved_chunks, iteration}"]

    SR --> RESP
    alRet --> RESP["respond node<br/>parse [SOURCE_N] → citations"]
    RESP --> END1([END])
    END1 --> ckpt2[(save checkpoint)]
    ckpt2 --> out["ask.py prints:<br/>▸ router → agent<br/>▸ tool_call: search_code/read_file/execute_code<br/>▸ iterations: N<br/>─── answer ───<br/>Citations"]

    classDef exec fill:#ffe0b2,color:#000,stroke:#e65100,stroke-width:2px
    style EX fill:#fff8f0,color:#000,stroke:#e65100
    style DK fill:#fff0e0,color:#000,stroke:#bf360c
    style EC fill:#fff8f0,color:#000,stroke:#e65100
```

**Key:** The two compiled graphs (main + executor) are connected only through the `execute_code` tool calling `executor.invoke()` — Pattern C (tool-wraps-subgraph). The main graph never references the subgraph as a node; the orchestrator just sees a tool that happens to run its own state machine internally.

## 4. Data Anatomy

```mermaid
flowchart LR
    subgraph ExecutorState["ExecutorState (subgraph-local TypedDict)"]
        direction TB
        t["task: str — what to verify/compute"]
        rp["repo: str | None — mounted at /repo"]
        lg["lang: str — 'python' | 'bash'"]
        cd["code: str — current attempt's script"]
        so["stdout: str — sanitized, ≤4KB"]
        se["stderr: str — sanitized, ≤4KB"]
        ex["exit_code: int"]
        to["timed_out: bool"]
        at["attempt: int — 1..4"]
        dm["duration_ms: int"]
    end

    subgraph SandboxResult["SandboxResult (dataclass)"]
        direction TB
        sex["exit_code: int"]
        sso["stdout: str (sanitized)"]
        sse["stderr: str (sanitized)"]
        sto["timed_out: bool"]
        sdm["duration_ms: int"]
        sok["ok: property = exit==0 and !timed_out"]
    end

    subgraph DockerCmd["docker run command anatomy"]
        direction TB
        d1["--rm — ephemeral, auto-cleanup"]
        d2["--network=none — no exfil"]
        d3["--read-only — immutable rootfs"]
        d4["--tmpfs /tmp:rw,size=64m,noexec — scratch"]
        d5["--memory 512m --cpus 1 — resource caps"]
        d6["--pids-limit=128 — fork-bomb guard"]
        d7["--cap-drop=ALL — no Linux capabilities"]
        d8["--security-opt=no-new-privileges"]
        d9["--user 65534:65534 — nobody"]
        d10["-v workdir:/work:ro — code"]
        d11["-v repo:/repo:ro — source (resolved symlink)"]
        d12["image timeout 30s python /work/main.py"]
    end
```

**Defense-in-depth (4 layers, verified by smoke tests):**

| Layer | Mechanism | Defends against | Smoke check |
|---|---|---|---|
| 0. Gateway | gateway guardrails on LiteLLM | Malicious *prompts* reaching the LLM | Discovered via injection test |
| 1. OS isolation | Docker namespaces + `--rm` ephemeral | Persistence, host filesystem access | `Docker available` |
| 2. Capability restriction | `--network=none`, `--read-only`, mem/cpu/pids caps, `--cap-drop=ALL`, `nobody` user | Exfiltration, resource exhaustion, privilege escalation | Network/FS/timeout/forkbomb checks |
| 3. Output sanitization | ANSI strip, binary detect, 4KB truncate, `<sandbox_output>` wrap, system-prompt rule | Prompt injection via stdout, context overflow | Sanitization + injection checks |

## 5. Module Dependency Graph

```mermaid
flowchart TD
    cfg[config.py<br/>REPOS_DIR, AGENT_MAX_ITER]

    subgraph sandbox
        sbb[base.py<br/>Sandbox protocol,<br/>SandboxResult,<br/>sanitize_output]
        sbd[docker.py<br/>DockerSandbox]
        sbb --> sbd
    end
    cfg --> sbd

    subgraph subgraphs
        ce[code_executor.py<br/>ExecutorState,<br/>write/run/fix/check,<br/>build_executor]
    end

    llm[agents/llm.py]
    llm --> ce
    sbd --> ce

    subgraph tools
        tec[execute_code.py]
        ti[__init__.py<br/>ALL_TOOLS += execute_code]
    end
    sbd --> tec
    ce --> tec
    tec --> ti

    al[nodes/agent_loop.py<br/>+ execute_code in prompt<br/>+ &lt;sandbox_output&gt; tag]
    ti --> al

    df[sandbox/Dockerfile<br/>python:3.12-slim<br/>+ regex pytest pyyaml]

    smoke[scripts/smoke_test.py<br/>+20 sandbox/executor checks]
    texec[scripts/test_executor.py<br/>layered manual tests]
    sbd --> smoke
    ce --> smoke
    sbd --> texec
    ce --> texec
    tec --> texec

    style sbb fill:#ffe0b2,color:#000
    style sbd fill:#ffe0b2,color:#000
    style ce fill:#ffe0b2,color:#000
    style tec fill:#ffe0b2,color:#000
    style df fill:#ffe0b2,color:#000
    style texec fill:#ffe0b2,color:#000
```

## 6. Phase 2.5 vs Phase 3 — What Changed

| Aspect | Phase 2.5 | Phase 3 |
|---|---|---|
| Agent capability | Read-only (search, read, grep, graph) | + **Execute** (write & run code in sandbox) |
| Multi-agent pattern | None — single agent with tools | **Supervisor/worker**: orchestrator → CodeExecutor subgraph |
| Agent tools | 7 | **8** (+ `execute_code`) |
| Subgraphs | 0 | **1** (`code_executor`: own state, own loop ≤4, own prompt, no checkpointer) |
| Sandbox | — | DockerSandbox: ephemeral container per run, full hardening |
| Languages executed | — | Python, bash (Option A — no Java compile; verify *about* Java code via scripts) |
| Output handling | Tool results wrapped in `<retrieved>` | + `<sandbox_output>` for execute_code; ANSI/binary/truncate sanitization |
| Prompt-injection defense | `<retrieved>` + system prompt | + `<sandbox_output>` + gateway guardrails (discovered) |
| Smoke tests | 35 | **55** (+15 sandbox isolation, +5 subgraph) |
| New deps | — | None (uses host Docker; subprocess only) |
| New infra | — | `sandbox/Dockerfile` → `cto-sandbox:latest` image |
| Lines added | — | ~720 across 9 new files |

### Bugs found & fixed during Phase 3

| Bug | Fix |
|---|---|
| Test: repo-mount check asserted `'src' in sorted(listdir())[:5]` but dotfiles sort first | Check `os.path.exists('/repo/src')` directly |
| Test: fork-bomb assertion expected `exit≠0`, but contained bomb exits 0 in ~180ms | Assert `not timed_out` and `duration < 7s` instead |
| Injection test routed payload through LLM → blocked by gateway guardrails guardrail | Test sandbox layer directly (no LLM); document gateway as bonus defense layer |

### Measured

| Metric | Value |
|---|---|
| Container spin-up + run + teardown | ~120-200ms (trivial code) |
| Timeout enforcement | Killed at host `timeout+5s` (in-container `timeout 30s` + host fallback) |
| Fork bomb containment | Returned in 182ms, host unaffected (`--pids-limit=128`) |
| Smoke suite | 55/55 passing, ~24s total |
