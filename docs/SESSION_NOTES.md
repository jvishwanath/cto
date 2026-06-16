# Session Notes — Decisions & State

> Compact log of decisions, results, and gotchas. Read this to reconstruct context fast.
> Full plan: `LEARNING_PATH.md`. Diagrams: `PHASE<N>_ARCHITECTURE.md`.

---

## Status

| Phase | State | Git tag | Recall@1 / @5 |
|---|---|---|---|
| 0 — Baseline | ✅ Done | `phase-0` | 0.60 / 0.80 |
| 1 — Chunking + Hybrid + Rerank | ✅ Done | `phase-1` | 1.00 / 1.00 |
| 2 — LangGraph foundations | ✅ Done | `phase-2` | (agentic — multi-turn verified) |
| 2.5 — Code graph | ✅ Done | `phase-2.5` | 1877 sym, 11264 edges; 35/35 smoke tests |
| 3 — CodeExecutor + sandbox | ✅ Done | `phase-3` | Docker sandbox, Python/bash only; 55/55 smoke tests |
| 3.5 — Auto-ingest + Web UI | ✅ Done | `phase-3.5` | BM25, incremental, watcher, docs, repo_info, Gradio UI; ~83 tests |
| 4 — Memory + parallelism + HITL + query analysis | ✅ Done | `phase-4` | cache, Store, Send fan-out, summarize, interrupt(), **query_analysis (4-F)**; 10-node graph; 39 tests |
| 4.5 — Guards + evaluator + reset + clarify-v2 | ✅ Done | `phase-4.5` | 14-node graph; reset_turn-first; input_guard pre-LLM; multi-round per-token clarify backstop; evaluator retry≤2; output_guard; cache keyed on resolved query; 39+27 tests, 128 flag-combos compile |
| 4.6 — Observability (Phoenix) | ✅ Done | `phase-4.6` | OTel auto-instrument; 1 container; `🔍 trace` link; `make trace` |
| 5 — Multi-source + cascade | ✅ Done | `phase-5` | Confluence connector (24h version-diff), git-history tools, JIRA live lookup, retrieval cascade (code→docs→confluence), citation-aware abstain + relatedness bands, cross-source join; 18 tools; tests→tests/, full_index module, scripts/legacy/; 39+29+29 tests. **Diverged from plan** (Confluence-DC not GitLab; on-demand tools not webhook workers; MCP/Slack deferred) |
| 6 — Deploy + CLI + auth | ✅ Done | `phase-6` | `cto` REPL (rich/prompt_toolkit, ⏺/⎿, self-animating spinner, slash cmds) + `--remote` thin SSE mode; Dockerfile + compose `app` service (`./data`,`./hf-cache` mounts); `make docker-*`/`prewarm-reranker`/`chat-remote`/`gen-api-key`; bearer auth (`CTO_API_KEYS`) + Gradio login + `X-Forwarded-User` trust + oauth2-proxy `oidc` profile; SSE enriched (intent/scope/eval/trace_url); `.env.example` exhaustive; DEPLOY.md |
| 7 — Skills (pluggable playbooks) | ✅ Done | `phase-7` | Two-mode `DockerSandbox` (`run_shell` knobs + persistent `start/exec/stop/reap`); `cto-secaudit` image (semgrep/trivy/gitleaks/tfsec/checkov/bandit/pip-audit/osv + node/go/jdk/cargo toolchains, `secaudit-versions`); `make_run_shell_tool` (closes over mode/image/cid), `write_report` (path-jailed→`data/reports/`, auto-index), `ask_user` (interrupt()-in-tool, verified w/ PostgresSaver); registry (`data/skills/*.md`, fail-soft, `match/get`); `skill_runner` = `create_react_agent(prompt=preamble+body)` + container lifecycle + `get_stream_writer()` → live ⏺/⎿ via `custom` stream mode in CLI/UI/SSE; router explicit-name + trigger regex, `/skills`, `GET /skills`; `onboarding-tour` (no-sandbox) + `security-audit` (persistent) skills. 18-node graph; route=skill skips eval/guard/cache. **Diverged from plan:** prebuilt agent not hand-rolled; ask_user sentinel pattern dropped (interrupt() works in-tool). **Limits:** one ask_user per run; secaudit image not live-built yet |
| 8 — Code mutation (primitives → `/superdev` → skill remediation) | ✅ Done | `phase-8` | **8-A** `vcs/worktree.py` (create/attach/discard/list_orphans/reap; `data/superdev/<sess>/<repo>` on `cto/*` branch; src pristine, idempotent re-attach); **8-B** `vcs/pr.py` (`create_pr`: push -u + GL REST/GH `gh`-or-REST; refuses non-`cto/*`/protected/no-commits/bad-subject; `commit_all`/`commit_message` w/ `Co-Authored-By: CTO`); `make_create_pr_tool(wt)` factory (LLM can't pick branch); **8-C** `agents/tools/host.py` (`build_host_tools(wt|None, roots)`: host_shell w/ destructive-regex→`interrupt()`, host_read unrestricted, host_write/edit `_jailed()` to roots, `docker_run(network=True)`, commit, create_pr) — NOT in `TOOLS_BY_NAME`; `agents/superdev_graph.py` (5-node 2nd graph, raises if flag off; inner `create_react_agent` on own checkpoint thread; interrupt bridge; relays `skill_event{tool_call,tool_result,text}` for live ⏺/⎿ + token stream); CLI `/superdev [repo]` (gates: `CTO_SUPERDEV_ENABLED` + `!--remote` + user-typed-only), `/superdev allow <dir>`, `/exit-superdev` (k/d/p), `⚡›` + red toolbar; **8-C′ shell-only** (no arg → cwd=$PWD, drops write/edit/commit/PR); **8-D** `SandboxCfg.worktree: bool` (requires persistent), `DockerSandbox.start(worktree=)` → `-v wt:/work:rw` replaces tmpfs, `skill_runner` owns wt lifecycle (deterministic session dir for replay; keep-if-commits else discard), `reap_orphans` += worktrees; `security-audit.md` gains `worktree: true` + `create_pr` + Phase-11 body (consent → edit /work → commit → build → revert-on-fail → PR). 33/33 tests; `make test-phase8`, `make worktree-reap`. **Diverged from plan:** added shell-only mode (ops use-case surfaced); host_shell cap 16 KB not 50 KB (large-table re-render hung @ 130s); inner agent streams tokens. **Untested live:** real `create_pr` against remote; full security-audit Phase-11 run. |
| 8.5 — `/superdev` as RAG-aware coding assistant | ✅ Done | `phase-8.5` | **8.5-A** `_RAG_TOOL_NAMES` (12 read-only tools: search_code/find_symbol/find_callers/callees/grep/read_file/repo_info/git_log·show·blame/find_commits_for_jira/search_docs) lazily imported into `build_host_tools` — both worktree & shell-only modes; one-way (read-only graph still doesn't import host.py). **8.5-B** `agents/playbooks.py` (`list/load/render`, name-validated); `data/playbooks/{tdd,systematic-debugging,plan-then-execute,verify-before-done}.md` (methodology prose for the 8.5 toolset; landing zone for ported superpowers content); `build_superdev_graph(playbooks=)` prepends `render()` block; CLI `/superdev [repo] [pb…]`, `⚡› /playbook <name>` mid-session (rebuilds graph; wt/todos/history survive), `/playbook off`, `/playbooks`; toolbar `📖 …`. **8.5-C** `host_glob(pattern)` (recursive, .git-skipped, jailed, ≤500), `host_apply_patch(diff)` (unified diff via `patch -p1`, every `+++` target jailed), `todo(set\|add\|done\|show)` (closes over caller-owned `todo_state` list — same object as `sd["todos"]` so toolbar reads it; emits `skill_event{kind:"todo",items}`); CLI `_todo_panel` boxed checklist + toolbar `☑ N/M`; UI trace block. `_system_prompt`: "never end a turn with uncommitted changes — last calls MUST be `commit`→`ask_user`"; "exit 127 → STILL commit+ask"; "prefer host_glob/find_symbol over ls chains". `superdev_loop`: `stream_mode += "custom"` (relay todo events); cli skip bulk-delta replay for `superdev_loop`. 39/39 tests (was 33). **Origin:** "make /superdev a write-focused coding assistant" + "can I drop superpowers in" → playbooks are the port target (strip Agent/Task, keep methodology). **Deferred:** 8.5-D subagents. |
| 8.5-G — wrap regular's quality pipeline around `/superdev` | ✅ Done | `phase-8.5-G` | **Wrapper not unification.** `agents/_turn_kind.py` NEW: `classify_turn(new_msgs)` walks `AIMessage.tool_calls` against `READ_TOOLS` (12 native + ask_user/todo) + `READ_MCP_BASES` (read_file/fetch/search_files/list_*/sequentialthinking) → `'read'|'action'`; MCP `<server>__<tool>` prefix-stripped. `state.turn_kind: str` added. `superdev_graph.py` rewired: `reset → input_guard → load_memories → superdev_loop → _eval_route → (evaluator | skip) → output_guard → respond → [should_summarize?] → summarize → save_memories → END`. SAME node fns as `graph.py` (`memories/evaluator/guards/summarize/respond` — 0 reimpl). `superdev_loop` post-stream: calls `classify_turn` + `_extract_chunks` (only search/search_code/search_docs ToolMessages, same pattern as agent_loop). `reset` wraps `reset_turn(state)` + stamps `route='superdev'`. **Mode-aware skips:** `nodes/evaluator.py` skips superdev action; skips evidence-free turns (no chunks AND no `[SOURCE_N]`); forces `will_retry=False` for `route='superdev'` (wrapper has no retry edge — eliminated misleading "retry #1" trace). `nodes/memories.py save_memories` skips superdev non-read (no "Committed sha-abc" garbage in store). `nodes/summarize.py`: new `_safe_split(messages, keep_recent)` walks back past `AIMessage(tool_calls)`↔`ToolMessage` chains so summary never severs a pair (Anthropic 400 prevention; benefits both graphs). **System prompt:** added explicit **Repo scope rule** (omit `repo=` unless user named one — fixed agent always defaulting `repo='<cwd-repo>'` from cwd) + **`[SOURCE_N]` + footnote convention** for read turns (mirrors `agent_loop.py:78`). **Cache mode-scoping** (`memory/cache.py`): `mode` kwarg on `cache_lookup/cache_store/store_eligible/_point_id/_normalize`; UUID5 = `mode::query` so superdev/regular never collide; Qdrant server-side `Filter(must=[FieldCondition(mode)])` for superdev, `Filter(should=[…, IsEmptyCondition('mode')])` for regular (backward-compat with pre-8.5-G entries); `limit=3`; payload stamps `mode`; `store_eligible` rejects `route='superdev' and turn_kind!='read'`. **CLI** (`api/cli.py`): `_run_turn(..., mode='regular')`; superdev branch passes `mode='superdev', use_cache=use_cache` (was hardcoded False); CRITICAL fix — renamed `for mode, data in ev` → `for kind, data in ev` (the loop var shadowed the function param, so `cache_store(..., mode=mode)` was writing under `'custom'/'messages'/'updates'` — broke BOTH regular AND superdev caching introduced earlier this PR); `text` skill_event drops `Padding(Markdown(streaming, …))` overlay → `spin.note = "streaming (N chars)"` only (superdev's inner agent echoed code/doc snippets while reasoning → flashed on screen); `tool_result` clears stale `streaming` + `spin.above` before next thinking round. UI + app got same rename prophylactically. **Case-insensitive parity** (`retrieval/graph_query.py`): `find_callers` + `find_callees` SQL used `name = %(name)s` AND `e.callee_name = %(name)s` — case-sensitive. LLM-lowercased `validateauthtoken` missed indexed `validateAuthToken`. Both now take `case_sensitive: bool = False` + use ILIKE by default (parity with `find_symbol` patched earlier); tool wrappers expose the kwarg. **Stdout silencing:** 30+ `print(...)` debug breadcrumbs across `nodes/{guards,evaluator,summarize,clarify,query_analysis,router,simple_rag}` + `memory/cache.py` → `log.debug/info/warning`. `grep -rn "^\s*print(" src/rag/agents src/rag/memory` → empty. **Net wrapper-attributable: ~260 LOC** (~150 superdev-only wiring/helpers; rest backward-compat widening of shared infra). 10/10 existing tests still pass. **Origin:** "Currently regular mode's graph pipeline is very rich — what does it take for superdev to get it?" → wrapper plan over unification (3.5 days, easier to revert per-phase if regression). **Diverged from initial plan:** added `mode`-shadow fix (critical regression in cache caused by my own kwarg threading), case-insensitive callers/callees (uncovered during live trace), text-overlay drop (user-reported visual flash), repo-scope prompt rule (user-reported wrong-repo defaults), citation directive (user noticed missing citations vs regular mode). **Untested live:** Postgres `_SKIP_ROUTES` superdev gate end-to-end (logic verified in smoke tests); MCP unknown read tools (unrecognized → 'action' fallback — flagged for follow-up); the `[1]` orphan-citation pattern user saw in screenshot (regex requires `[SOURCE_N]`, may have been a rendering artifact). **Known follow-ups:** unify tool-mutation classification across `_turn_kind.READ_TOOLS` ↔ `host._RAG_TOOL_NAMES` ↔ `mcp/tools._MUTATE_RE` (3 hand-maintained lists drift risk); extract `_extract_chunks` shared helper (3 copies); lift `_safe_split` to `langchain_core.messages.trim_messages`; per-route `gradable: bool` registration when 4th route lands. |
| 9 — Task scheduler | ✅ Done | `phase-9` | **9-A** `scheduler/jobs.py` (`Job`: `cron`\|`interval` × `skill`\|`query`\|`exec`; built-in 5-field cron — no croniter dep; `is_due`/`next_fire`; `load_jobs` fail-soft); `data/schedules.yaml.example`. **9-B** `scheduler/runner.py` `SkillScheduler` daemon (`_due`/`_next_sleep`/`_fire`, per-job worker thread, wall-clock cap, no-overlap), `_run_graph` (headless invoke, `tid=sched/<job>/<repo>/<ts>`), `_run_exec` (sandbox `cto-ops` \| host behind `CTO_SCHED_HOST_EXEC`, project-root-jailed), `run_job_now`, `status_rows`, `_append_log` → `data/logs/scheduler.log`; `store.py` `scheduled_runs` Postgres table; `SandboxCfg.env_passthrough/mounts` → `-e`/`-v…:ro`; `Dockerfile.ops`; lifespan wired. **9-C** `state.headless`/`headless_answers`; `clarify` auto-picks-all (factored `_apply_answer`); `make_ask_user_tool(headless, answers)` → matched default or `skip`; skill_runner bridge auto-`skip`. **9-D** `hooks.py` `diff_prev` → `-delta.md`, `notify` (`slack://` w/ `SLACK_WEBHOOK_URL`, `log://`). **9-E** `GET /jobs` Accept-negotiated HTML dashboard (▶fire, runs, log-tail, 30s refresh) \| JSON; `POST /jobs/{n}/run`; Gradio `⏰ Jobs` modal; CLI `/jobs`; `make jobs-run/log/tail`, `docker-ops`. `scripts/jobs/pull_repos.py` ref exec job. 29/29 tests; phase8 still 33/33. **Diverged from plan:** added `exec:` job kind + `env_passthrough`/`mounts` (generic ops scripts use-case — see [[phase9-exec-jobs]]); built-in cron not croniter; HTML `/jobs` page + Gradio modal beyond spec. **Untested live:** Slack POST, full nightly-secaudit fire. |
| 10 — Distribution: binary CLI + setup wizard | ✅ Done | `phase-10` | **Slice 1 (wizard + XDG)** `src/rag/cli/` new pkg: `_config.py` (XDG layout: `$XDG_CONFIG_HOME/cto/config.yaml` + `credentials.toml` 0600; atomic write via `.tmp`+`os.replace`; dataclass-typed sections — Remote/LocalInfra/Integrations/Prefs; dotted-key TOML flatten/expand). `_health.py` (stdlib-only probes: CTO server, LLM gateway, Qdrant, Postgres via psql-or-TCP, docker info, JIRA `/myself`, Confluence `/user/current`; `redact()` for hostnames/JWTs/AWS keys/$HOME paths). `setup.py` (single-pass wizard via getpass + input; three branches remote/local/skip; defaults pre-filled from existing config; `<existing>` sentinel keeps secrets blank-to-preserve; atomic persist at end; Ctrl-C anywhere before persist = no on-disk change). **Slice 2 (compose bootstrap)** `cli/compose.py` (`pull/up/down/ps/logs/status` subcommands; `_wait_healthy` polls `docker compose ps --format json` until all 3 svcs report `healthy` or 90s timeout; svc crash → tail 50 lines of logs; hand-rolled dispatch — no argparse — for tiny startup). `packaging/compose.cto-infra.yaml` (slim: qdrant + postgres + phoenix only; `${CTO_DATA_DIR}` templated for XDG bind-mounts; per-svc healthchecks). Setup wizard's local branch hooks `cmd_pull`+`cmd_up` after persist. **Slice 3 (binary)** `packaging/pyinstaller_cto.spec` (`--onefile` thin-client; `excludes=[torch, transformers, langchain*, langgraph*, qdrant_client, psycopg, tree_sitter*, fastapi, uvicorn, gradio, openai, anthropic, ...]` cuts ~650 MB → final ~30-50 MB; `hiddenimports` enumerates `rag.cli.*` + rich + prompt_toolkit; `datas` ships `compose.cto-infra.yaml`; `upx=False` for Gatekeeper compat; `target_arch=None` native-only). Makefile `binary/binary-mac-arm64/binary-mac-x86_64/binary-clean` with per-arch host guards. `pyproject.toml [binary]` extras = `pyinstaller>=6.0`. `packaging/README.md` documents build flow. **Slice 4 (doctor)** `cli/doctor.py` (per-mode connectivity table; file-perm checks for config + credentials; redacted output by default — `--unsafe` opt-in shows full URLs/paths; exit 0/1/2 contract). **CLI dispatch** `api/cli.py:main()` short-circuits `setup`/`doctor`/`compose` BEFORE heavy graph/tracing imports (cold-start ~150ms vs ~2s for chat path); `cto chat` + `cto [question]` unchanged. **Scope cuts (per user direction):** macOS arm64+x86_64 only (no Linux/Windows v1); offline distribution (no GitHub Releases/Homebrew/auto-update); Docker required for local-mode (no native-store fallback); single full wizard re-run (no sub-wizards); single shared `cto` compose project (no per-user namespacing). **Smoke verified:** all 6 new modules parse + import; subcommand routing recognizes setup/doctor/compose; atomic config write + 0600 perms confirmed; round-trip config + credentials preserves all fields; cto doctor exit 2 on no-config; cto compose status against live dev stack: qdrant ✓ postgres ✓ phoenix ✓; bundled compose extracted to `$XDG_DATA_HOME/cto/compose/` (2143 bytes); `_parse_ps_json` handles both line-JSON + array-JSON compose formats. **Untested live:** `make binary` actual PyInstaller build (requires `pip install ".[binary]"` first); full e2e of `./dist/cto-darwin-arm64 setup` → `compose up` → `chat` flow; codesigning (deferred to 10.1). **Known follow-ups:** cto-app image bundled in compose (needs registry agreement); Linux + Windows builds; `cto upgrade` (blocked by offline scope); codesigning + notarization (10.1); `cto skill install <url>` marketplace (blocked by Sprint 1 skill hardening). Net ~1380 LOC across 9 new + 3 modified files. **Origin:** "make this into a binary for cli usage with a wizard taking input of required env vars" — sketched per-phase then implemented. |
| 11 — Sprint 1: Skills hardening (security) | 🗓 Planned | — | `docs/SPRINT1_SKILLS_HARDENING.md` — threat model, 6-file changes, 11 unit tests, ~770 LOC / ~5d estimate, 3 open questions. Blocks `cto skill install` marketplace work. |

---

## Locked Decisions

| Area | Decision | Notes |
|---|---|---|
| LLM gateway | corp LiteLLM proxy (set `LITELLM_URL` in `.env`) | App uses OpenAI SDK pointed here. No local LiteLLM container |
| Embedding model | `text-embedding-3-large` @ 1024 dims | Voyage not available in gateway |
| Agent model | `claude-sonnet-4-6` (Vertex via gateway) | Fallback: `claude-opus-4-7` |
| Router model | `gpt-4.1-mini` (Azure via gateway) | Cheapest capable |
| Fast/fanout model | `gemini-3.1-flash` | For Phase 4 deep_research |
| Reranker | `bge-reranker-v2-m3` local (sentence-transformers) | No Cohere in gateway; runs in-process |
| Vector DB | Qdrant (Docker, port 6333) | Dense + sparse, RRF fusion native |
| Sparse vectors | Hand-rolled term-freq vocab (`data/vocab.json`) | Not true BM25; good enough for now |
| Test repos | `acme-auth`, `acme-api`, `acme-jwks`, `acme-gateway`, `xt-auth` (spoof), `cto` (self) | Java/Spring Boot + this Python repo |
| Docker runtime | Docker Desktop (`desktop-linux` context) | Had to fix stale `DOCKER_HOST` → another user's podman socket |
| **Tracing** | Arize Phoenix (OTel/OpenInference), `localhost:6006` | Langfuse rejected: v3 needs 4 containers; v2 SDK incompatible with langchain ≥1.0 |
| **Product name** | **Code / Trace / Origin** (CTO) — *"From signal to source — cited to the line."* Header in Space Grotesk; slashes muted | Scope expanding to logs/metrics in later phases. Prior candidates: Traceline, Sourcetrace, Lodestar |
| **Repo ambiguity** | LLM resolves only when unambiguous; else `needs_human` → clarify. Lexical backstop fires when LLM committed to 1-of-N. **No naming heuristics.** | Family-prefix repair tried & removed — would override explicit choices and break with `xt-auth`-style names |
| Cache store | `store_eligible()` — any turn; key = resolved query; excludes detailed/expand/blocked/abstained | Replaced `fresh` (turn-1-only) gate |
| Clarify cap | `MAX_CLARIFY_ROUNDS=3` (counted via `_round_count`, ignores `repo_token:*` markers); >cap → single `repo_bulk` free-form ask | |
| **Terminal client** | `cto` (rich + prompt_toolkit). Local mode = in-process graph; `--remote`/`CTO_REMOTE` = pure httpx SSE client (zero ML deps) | Same `⏺`/`⎿` rendering both modes; remote loses per-token streaming (server uses `updates` only) |
| **Auth** | Off-by-default. `CTO_API_KEYS` (server, comma list, sha256+`hmac.compare_digest`) → Bearer on `/query` `/sources` `/sessions`; `CTO_UI_USER/PASSWORD` → Gradio login; `CTO_TRUST_FORWARDED_USER` → accept `X-Forwarded-User` from oauth2-proxy/ALB and override `req.user_id` | OIDC sketch: compose `--profile oidc` (oauth2-proxy upstream→app). `/health` always open |
| **Reranker on EC2** | NOT baked into image. `make prewarm-reranker` → `./hf-cache/`, bind-mounted at `/hf-cache`, loaded `local_files_only=True` | ~1.1 GB; rsync from laptop if EC2 can't reach HF |
| **Skills sandbox** | `mode: ephemeral` (default) — `--rm` per call, `--read-only --network=none`. `mode: persistent` — one named container per skill run, writable rootfs + network opt-in, `/repo:ro` always, `cid` checkpointed; `reap_orphans()` in lifespan + `make sandbox-reap`. NEVER host-exec / devbox. | Persistent is for discover-then-install (security-audit Phase 0/2); ephemeral for one-shot scanners; `none` for read-only skills |
| **Skill capability boundary** | `tools_allowed` in frontmatter is enforced by `build_skill_tools()` — a skill that doesn't list `run_shell_command` CANNOT shell, regardless of prompt content | The prompt is a request; the toolset is the permission |
| **Reports** | `write_report` path-jails to `data/reports/<repo>/<flat-name>` (no `..`/subdir/abs, ≤2 MB), then `index_doc()` so follow-ups retrieve from it | Repo itself is never written |

---

## Eval Golden Set

5 questions in `eval/golden_set.jsonl` (need ~25 more for robustness).
Phase 0 failure case fixed in Phase 1: "What endpoint handles device enrollment?"

---

## Deferred to Later Phases

| Item | Deferred to | Why |
|---|---|---|
| GitLab/Confluence connectors | Phase 5 | Local dir walk sufficient for now |
| Incremental indexing (`make index-add REPO=X`) | Phase 5 | Full re-index ~2 min, acceptable |
| Pluggable `service_metadata/` (Go/Node/Python/Helm + `.ragmeta.yaml`) | Phase 5 | Current impl is Spring-only |
| Local LLM (Ollama/Qwen3) for router/offline | Phase 3+ | Discussed, not needed yet |
| ~~Consolidate `hybrid_search()` duplication~~ | ~~Phase 2~~ | ✅ Done in Phase 2 refactor |
| Replace hand-rolled sparse vocab with Qdrant native BM25 (`Modifier.IDF`) | Phase 5 | Removes `vocab.json` dependency; see limitation below |
| **Go remote-CLI** (`cmd/cto/`, lipgloss+glamour+bubbletea over `/query` SSE) | Phase 6+ | Single static binary, cross-compile all platforms from one host (~8–15 MB). Hold until CLI UX settles; alt: `shiv` zipapp if Python 3.11+ is acceptable on target machines |
| `PHOENIX_PUBLIC_HOST` env for browser-facing trace links | Phase 6 | Container emits `http://phoenix:6006/...`; needs rewrite for laptop browsers |

---

## Follow-ups / Deep-Dives Owed

- [x] **Repo rename `agentic-rag` → `cto`** — DONE pre public
      release (2026-06-16). pyproject `name=cto`; `app.py`
      service id; PHOENIX_PROJECT default; sandbox image tag;
      postgres `container_name`; Makefile `docker exec`; all
      docs swept; Phase 10 README updated. Local dir `mv` is the
      user's separate step (`data/repos/<self>` symlink will
      need re-pointing after). Data is safe (all bind-mounts to
      `./data/…`).

**After Phase 2.5 build is working**, revisit these concepts:
- [ ] tree-sitter query syntax (`.scm` patterns) — declarative call-site extraction
- [ ] Java import resolution → fully-qualified names (improves edge accuracy)
- [ ] Inheritance edges (`extends`/`implements`) — interface → impl traversal
- [ ] Recursive CTE cycle handling deep-dive (path array mechanics)
- [ ] Edge resolution timing: index-time vs query-time trade-offs
- [ ] Cross-repo HTTP edges: bridging `restTemplate.post()` → `@PostMapping` across services

---

## Known Limitations

| Limitation | Impact | Workaround / Fix |
|---|---|---|
| **Sparse vocab frozen at index time** — `data/vocab.json` is built during `make index-v1`. New terms in repos added later won't have sparse-vector indices | Sparse/keyword search misses new identifiers; dense search still works | Re-run `make index-v1` after adding repos. Permanent fix: Qdrant native BM25 sparse (no vocab file) |
| tree-sitter grammars: Java/Py/Go/JS/TS only | Other langs fall back to naive chunking (Phase 0 quality) | Add grammar to `pyproject.toml` + `_GRAMMAR_MAP` + `_SYMBOL_TYPES` in `chunking/code.py` |
| `service_metadata.py` is Spring-specific | Non-Spring repos get empty `depends_on`/`layer` | Pluggable extractors in Phase 5 |
| No `search_docs` tool | Agent can't search Confluence/PDFs | Add when docs collection exists (Phase 5) |
| Semantic cache: lookup any turn, **store any turn** keyed on RESOLVED query (post-clarify); `detailed`/expand-requests/blocked/abstained excluded; verbosity NOT in cache key | A `terse` ask may return a cached `normal` answer (accepted); agent-route turn-N answers may contain history-referencing phrasing | Phase 7: add `repo_scope` to cache key; strip history-phrasing in respond.py |
| `cache_purge_expired()` never called outside tests | Expired entries accumulate in `query_cache` collection | `make cache-purge` manual; wire into FastAPI lifespan |
| Phoenix `🔍 trace` link → sessions list, not exact trace | One extra click to find the turn | `_Tracer.capture_trace_id()` exists; call from inside stream loop for deep links |
| `push_scores()` writes span *events*, not Phoenix annotations | Scores visible in Events tab, not filterable in trace table | Use Phoenix annotations API (`POST /v1/span_annotations`) |
| Self-symlinked repo (`data/repos/cto → .`) won't auto-reindex on edit | Watcher refuses self-loop watch (intentional) | `make index-add REPO=cto` after big changes |
| `_EXPAND_RE` won't catch "what about X" / "and Y?" follow-ups | Those go through full pipeline (usually fine — agent has history) | Acceptable; only bare expand requests short-circuit |

---

## Environment Gotchas

| Issue | Fix |
|---|---|
| Corp LiteLLM gateway has its own guardrail layer — blocks LLM inputs flagged as `toxicity`/prompt-injection with HTTP 500 | This is a *feature* (extra defense layer). But it means tests that send injection-looking strings to the LLM will fail. Test injection defenses at the sandbox layer instead |
| Postgres collation mismatch (glibc drift between image versions) blocks `CREATE DATABASE` | `ALTER DATABASE template1 REFRESH COLLATION VERSION;` then `… rag …;` then create |
| Gradio 6 `gr.Group(elem_id=X)` renders nested divs **both** with `id=X`; `transform` on either traps `position:fixed` | Use `elem_classes` for the inner card; outer overlay = `inset:0` flex (no transform); place modal AFTER chatbot in DOM |
| Gradio queue default `concurrency_limit=1` — one long `_chat` blocks all tabs | `.then(_chat, ..., concurrency_limit=8, concurrency_id="chat")` + `ui.queue(default_concurrency_limit=16)` |
| Mermaid in dark mode: `fill:#<pastel>` without `color:` → white text on light bg | Always pair `fill:` with `color:#000`; use `rgba(...,0.12)` for sequence-diagram `rect` tints |
| Phoenix `/projects/<x>` route wants opaque GraphQL node ID, not project name | `_resolve_project_id()` does `GET /v1/projects` → cache name→ID |
| `create_react_agent` API drift: `state_modifier=` removed; whole thing moving to `langchain.agents.create_agent` | Use `prompt=` kwarg. **`agent_loop.py:build_prebuilt_agent()` still uses `state_modifier=` — broken on current LangGraph; harmless because `AGENT_PREBUILT=false` by default.** Verified `interrupt()` works directly inside a tool function with `PostgresSaver` (no sentinel pattern needed — Phase 7-D) |
| `DOCKER_HOST` pointed at another user's podman socket | `unset DOCKER_HOST` + `docker context use desktop-linux`; remove from `~/.zshrc` |
| SSL `CERTIFICATE_VERIFY_FAILED` to HuggingFace/external | `export REQUESTS_CA_BUNDLE=<corp-root-ca.pem>` and `SSL_CERT_FILE=...` |
| `find -type f` returns nothing on linked repos | macOS `find` quirk with `-o` precedence; use parens or per-dir search |
| Python deps | Use venv: `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"` |

---

## Conventions

- Each phase ends with: commit → `git tag phase-N` → write `docs/PHASE<N>_ARCHITECTURE.md` (5 Mermaid diagrams: system, flow, deps, data anatomy, vs-prev table)
- Phase 0 scripts kept alongside Phase 1 (`*_v1.py`) for comparison; will consolidate in Phase 2
- `make index-v1` wipes + rebuilds Qdrant collection (idempotent, safe to re-run)

---

## How to Resume

```bash
cd <your-clone-of-cto>
claude --continue          # or --resume to pick session
# or fresh session: say "Phase 2 — let's discuss"
```
