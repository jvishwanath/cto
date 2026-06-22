.PHONY: setup infra down sync-repos fixtures index index-v1 query query-v1 eval eval-v1 \
        preflight serve ask ask-prebuilt purge-sessions clean test test-full test-phase4 \
        test-phase4.5 test-skills test-web test-jira test-mcp mcp-list mcp-status \
        index-graph index-all index-add index-rm index-doc \
        index-docs sandbox-image sandbox-check test-exec watch test-ui \
        trace cache-purge cache-stats confluence-sync confluence-status \
        test-confluence chat chat-remote \
        docker-build docker-up docker-down docker-logs docker-shell \
        docker-exec docker-restart prewarm-reranker \
        sandbox-secaudit sandbox-secaudit-check sandbox-reap \
        binary binary-mac-arm64 binary-mac-x86_64 binary-clean

# Use the project venv if it exists; fall back to system python3.
PY := $(if $(wildcard .venv/bin/python3),.venv/bin/python3,python3)

# First-time setup
setup:
	cp -n .env.example .env || true
	$(PY) -m pip install -e ".[dev]"

# Start local infra ONLY (Qdrant + Postgres + Phoenix), app runs on host.
infra:
	docker compose up -d --remove-orphans qdrant postgres phoenix

# Stop everything
down:
	docker compose down

# ── Containerized deployment (EC2) ──────────────────────────────────
# Full stack: infra + app container.  See docs/DEPLOY.md.
docker-build:
	docker compose build app

docker-up:
	mkdir -p data/repos data/docs data/connectors hf-cache
	docker compose up -d --remove-orphans

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f --tail=200 app

docker-shell:
	docker compose exec app bash

# Run any make target INSIDE the app container, e.g.:
#   make docker-exec T=index-all
#   make docker-exec T="confluence-sync SPACE=ENG"
docker-exec:
	docker compose exec app make $(T)

# Restart only the app (after code change + rebuild).
docker-restart:
	docker compose up -d --no-deps --build app

# Generate an API key (paste into CTO_API_KEYS on server,
# CTO_API_KEY on clients).
gen-api-key:
	@$(PY) -c "import secrets;print(secrets.token_urlsafe(32))"

# Latency benchmark — 10 scenarios, per-node timing, ranked.
#   make bench                 — full run (incl. research fan-out)
#   make bench SKIP=research   — skip the slow compare scenario
#   make bench DETAIL=1        — per-node breakdown for each
#   make bench TRACE=1         — measure Phoenix overhead (~+6%)
bench:
	UI_ENABLED=false $(PY) scripts/bench_latency.py \
		$(if $(SKIP),--skip $(SKIP),) $(if $(DETAIL),--detail,) \
		$(if $(NO_CACHE),--no-cache,) $(if $(TRACE),--trace,)

# Pre-download the reranker model into ./hf-cache (mounted at /hf-cache).
# Run ONCE on the EC2 host before first `docker-up`; the app container
# will then load it via local_files_only=True with no network call.
prewarm-reranker:
	mkdir -p hf-cache
	docker run --rm \
	  -e HF_HOME=/hf-cache \
	  -e SENTENCE_TRANSFORMERS_HOME=/hf-cache/sentence-transformers \
	  $(if $(REQUESTS_CA_BUNDLE),-e REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt,) \
	  -v "$$(pwd)/hf-cache:/hf-cache" \
	  cto-app:latest \
	  python -c "from sentence_transformers import CrossEncoder; \
CrossEncoder('BAAI/bge-reranker-v2-m3'); print('cached → ./hf-cache')"

# Thin remote CLI against a running server (default localhost:8000).
#   make chat-remote R=http://ec2-host:8000
chat-remote:
	PYTHONPATH=src $(PY) -m rag.api.cli --remote $(or $(R),http://localhost:8000) \
		$(if $(S),--session $(S),) $(if $(U),--user $(U),)

# Clone repos into data/repos (run once)
sync-repos:
	mkdir -p data/repos
	@echo "Symlink or clone your repos into data/repos/"
	@echo "Example: ln -s ../../my-service data/repos/my-service"
	@echo "Or use the bundled fixtures:  make fixtures"

# Initialize the synthetic test repos (tests/fixtures/) as real git
# checkouts and link them into data/repos/. Idempotent. Run before
# `make test` on a fresh clone.
fixtures:
	@mkdir -p data/repos
	@for r in acme-auth acme-api; do \
	  d=tests/fixtures/$$r; \
	  if [ ! -d "$$d/.git" ]; then \
	    git -C "$$d" init -q -b main && \
	    git -C "$$d" config user.email "fixture@example.com" && \
	    git -C "$$d" config user.name  "fixture" && \
	    git -C "$$d" add -A && \
	    git -C "$$d" commit -q -m "fixture: initial"; \
	    echo "  init  $$d"; \
	  fi; \
	  if [ ! -e "data/repos/$$r" ]; then \
	    ln -s "$$(pwd)/$$d" "data/repos/$$r"; \
	    echo "  link  data/repos/$$r → $$d"; \
	  fi; \
	done
	@echo "✓ fixtures ready — run 'make index-all' then 'make test'"

# Phase 0: Naive indexing
# index:
# 	$(PY) scripts/legacy/index_repos.py

# Phase 1: AST-aware + hybrid indexing
index-v1:
	$(PY) scripts/index_repos_v1.py

# Phase 2.5: Build code graph (symbols + call edges) in Postgres
index-graph:
	$(PY) scripts/index_graph.py

# Phase 3: Build the sandbox image (pre-baked Python deps)
sandbox-image:
	docker build -t cto-sandbox:latest -f sandbox/Dockerfile sandbox/

# Phase 7-B: security/dependency-audit skill image — cross-stack
# scanners + language toolchains. Big build (~10 min, ~2 GB).
#   make sandbox-secaudit          — build
#   make sandbox-secaudit-check    — print every tool's version
sandbox-secaudit:
	docker build -t cto-secaudit:latest \
	  -f src/rag/sandbox/Dockerfile.secaudit src/rag/sandbox/

sandbox-secaudit-check:
	docker run --rm cto-secaudit:latest

# Reap orphaned cto-skill-* containers (abandoned interrupt(),
# server crash). Same as the FastAPI lifespan hook.
sandbox-reap:
	$(PY) -c "import sys; sys.path.insert(0,'src'); \
from rag.skills.runner import reap_orphans; \
print('reaped:', reap_orphans() or '(none)')"

# Phase 3: Layered CodeExecutor manual tests
#   make test-exec L=sandbox    (no LLM)
#   make test-exec L=subgraph   (LLM, write→run→fix)
#   make test-exec L=tool       (LLM)
#   make test-exec L=injection  (LLM)
#   make test-exec L=all
test-exec:
	$(PY) tests/test_executor.py $(or $(L),sandbox)

# Phase 3: Quick sandbox sanity check (no LLM)
sandbox-check:
	$(PY) -c "import sys; sys.path.insert(0,'src'); \
from rag.sandbox import DockerSandbox; \
s=DockerSandbox(); \
print('docker available:', s.available()); \
r=s.run('print(2+2)'); \
print(f'exit={r.exit_code} stdout={r.stdout!r} stderr={r.stderr!r} {r.duration_ms}ms')"

# Full reindex: vectors + graph
index-all: index-v1 index-graph

# Phase 3.5-C: Incremental repo index
#   make index-add REPO=acme-crypto                       → full repo
#   make index-add REPO=acme-auth FILES="a.java b.java"  → file-level
index-add:
	$(PY) -c "import sys; sys.path.insert(0,'src'); \
from rag.ingest.incremental import index_repo; \
files = '$(FILES)'.split() if '$(FILES)'.strip() else None; \
r = index_repo('$(REPO)', files=files); \
print(f'{r.repo}: mode={r.mode} files={r.files_processed} chunks={r.chunks} ' \
      f'symbols={r.symbols} edges={r.edges}' + \
      (f' [escalated: {r.escalation_reason}]' if r.escalated else ''))"

index-rm:
	$(PY) -c "import sys; sys.path.insert(0,'src'); \
from rag.ingest.incremental import delete_repo; \
print(delete_repo('$(REPO)'))"

# Phase 3.5-B: Index a single doc (PDF/MD/txt) into the 'docs' collection
index-doc:
	$(PY) -c "import sys; sys.path.insert(0,'src'); \
from rag.ingest.docs import index_doc; \
print(f'indexed: {index_doc(\"$(FILE)\")} chunks')"

# Phase 3.5-B: Index everything under data/docs/
index-docs:
	$(PY) -c "import sys; sys.path.insert(0,'src'); \
from rag.ingest.docs import index_docs_dir; \
import json; print(json.dumps(index_docs_dir(), indent=2))"

# Phase 0: Query
query:
	$(PY) scripts/legacy/query.py "$(Q)"

# Phase 1: Hybrid query
query-v1:
	$(PY) scripts/query_v1.py "$(Q)"

# Phase 0: Eval
eval:
	$(PY) eval/run_eval.py

# Phase 1: Eval (hybrid)
eval-v1:
	$(PY) eval/run_eval_v1.py

# Smoke test suite — verifies infra/storage/retrieval/graph/tools/router
test:
	$(PY) tests/smoke_test.py

# Smoke test + end-to-end agent queries (costs LLM tokens)
test-full:
	$(PY) tests/smoke_test.py --full

# Phase 4 test suite (cache, store, Send, summarize, clarify, graph integration)
test-phase4:
	$(PY) tests/test_phase4.py $(if $(FULL),--full,)

# Phase 4.5 test suite (guards, evaluator, reset_turn, graph reorder)
test-phase4.5:
	$(PY) tests/test_phase4_5.py

# Phase 5: Confluence connector tests (no network — mocks client/qdrant/pg)
test-confluence:
	$(PY) tests/test_confluence.py

# Phase 8: worktree, create_pr, host tools, superdev gate (no LLM/docker needed)
test-phase8:
	$(PY) tests/test_phase8.py

# Phase 8.5-E: generic SKILLS.md loader + critic edge cases
test-skills:
	$(PY) tests/test_skills_generic.py
	$(PY) tests/test_skills_generic_critic.py

# Web fetch/search (httpx-mocked, no network)
test-web:
	$(PY) tests/test_web_tools.py

# JIRA write tools (httpx-mocked, no live API)
test-jira:
	LITELLM_API_KEY=$(or $(LITELLM_API_KEY),sk-test) \
	OPENAI_API_KEY=$(or $(OPENAI_API_KEY),sk-test) \
	$(PY) tests/test_jira.py

# MCP client — config, tools, resources, prompts, events (all mocked)
test-mcp:
	$(PY) tests/test_mcp_config.py
	$(PY) tests/test_mcp_tools.py
	$(PY) tests/test_mcp_resources_prompts.py
	$(PY) tests/test_mcp_events.py

# List configured MCP servers + tool counts (no connection)
mcp-list:
	$(PY) -c "import sys; sys.path.insert(0,'src'); \
from rag.mcp.config import load_config; \
specs = load_config(); \
print(f'{len(specs)} server(s) configured'); \
[print(f'  {n:20s}  {s.transport:18s}  source={s.source}') \
 for n, s in sorted(specs.items())]"

# Probe each configured server: connect, list tools / resources / prompts
mcp-status:
	$(PY) scripts/mcp_status.py

# Run all phase test suites in sequence
test-all: test test-phase4 test-phase4.5 test-confluence test-phase8 \
          test-phase9 test-skills test-web test-jira test-mcp

# Reap abandoned cto-skill-* containers and un-pushed cto/* worktrees
worktree-reap:
	$(PY) -c "from rag.skills.runner import reap_orphans; print(reap_orphans())"

# Phase 9: scheduler (no LLM/docker needed for unit tests)
test-phase9:
	$(PY) tests/test_phase9.py

# Fire one job synchronously: make jobs-run JOB=nightly-secaudit
jobs-run:
	$(PY) -c "import json; from rag.scheduler import load_jobs, run_job_now, ensure_schema; \
ensure_schema(); \
jobs={j.name:j for j in load_jobs()}; \
j=jobs.get('$(JOB)'); \
print(json.dumps(run_job_now(j), indent=2, default=str) if j else 'unknown job: $(JOB) — have: '+', '.join(jobs))"

# Tail recent scheduled runs (Postgres scheduled_runs table)
jobs-log:
	$(PY) -c "import json; from rag.scheduler import list_runs; \
print(json.dumps(list_runs(20), indent=2, default=str))"

# Follow the human-readable per-fire log
jobs-tail:
	@mkdir -p data/logs && touch data/logs/scheduler.log
	tail -n 200 -f data/logs/scheduler.log

# Build the ops image (cloud CLIs + boto3) for exec: jobs / ops skills
docker-ops:
	docker build -f sandbox/Dockerfile.ops -t cto-ops:latest .

# Phase 3.5-E: Playwright e2e test of the Gradio UI (server must be running)
test-ui:
	$(PY) tests/test_ui_playwright.py "$(or $(URL),http://127.0.0.1:8000)" "$(or $(Q),where is validateCSR defined?)"

# Verify Qdrant + Postgres (and Phoenix when enabled) are reachable
# before launching the host-side app. Honors QDRANT_URL / POSTGRES_DSN
# / PHOENIX_HOST from .env. Skip with SKIP_PREFLIGHT=1.
preflight:
	@if [ "$(SKIP_PREFLIGHT)" != "1" ]; then $(PY) scripts/preflight.py; fi

# Phase 2/3.5: Run the FastAPI service (LangGraph agent + SSE + watcher)
serve: preflight
	$(PY) -m uvicorn --app-dir src rag.api.app:api  --port 8000

# Phase 3.5-D: Run the filesystem watcher standalone
watch:
	$(PY) scripts/watch.py

# Interactive terminal chat (Claude-Code-style REPL).
#   make chat            — fresh session
#   make chat S=abc123   — resume session
chat: preflight
	UI_ENABLED=false PYTHONPATH=src $(PY) -m rag.api.cli \
		$(if $(S),--session $(S),) \
		$(if $(U),--user $(U),) $(if $(NO_CACHE),--no-cache,)

# Phase 2: Ask the agent directly via CLI (multi-turn with --session)
#   make ask Q="..." S=sess-1 V=terse
ask:
	$(PY) scripts/ask.py "$(Q)" \
		$(if $(S),--session $(S),) \
		$(if $(U),--user $(U),) \
		$(if $(filter terse,$(V)),--terse,) \
		$(if $(filter detailed,$(V)),--detailed,) \
		$(if $(NO_CACHE),--no-cache,)

# Phase 2: Same question via prebuilt create_react_agent
ask-prebuilt:
	AGENT_PREBUILT=true $(PY) scripts/ask.py "$(Q)" $(if $(S),--session $(S),)

# Purge LangGraph conversation memory (checkpointer).
#   make purge-sessions          → wipe all sessions
#   make purge-sessions S=test12 → wipe one session
purge-sessions:
ifeq ($(S),)
	docker exec cto-postgres psql -U rag -d rag -c \
		"TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes;"
	@echo "Purged ALL sessions."
else
	docker exec cto-postgres psql -U rag -d rag -c \
		"DELETE FROM checkpoints WHERE thread_id = '$(S)'; \
		 DELETE FROM checkpoint_blobs WHERE thread_id = '$(S)'; \
		 DELETE FROM checkpoint_writes WHERE thread_id = '$(S)';"
	@echo "Purged session: $(S)"
endif

# Phase 5: Confluence sync (manual). Scheduler runs in `make serve` lifespan.
#   make confluence-sync                → all configured spaces, version-diff
#   make confluence-sync SPACE=ENG     → one space
#   make confluence-sync FORCE=1        → re-index regardless of version
#   make confluence-status              → per-space last_run + counts
confluence-sync:
	$(PY) scripts/confluence_sync.py \
		$(if $(SPACE),--space $(SPACE),) \
		$(if $(FORCE),--force,)

confluence-status:
	$(PY) scripts/confluence_sync.py --status

# Wipe all chunks + state for one space (next sync re-indexes from scratch)
confluence-reset:
	$(PY) scripts/confluence_sync.py --reset $(SPACE)

# Phase 4.6: Open Phoenix trace UI (resolves project name → opaque ID)
#   make trace            → project traces
#   make trace S=sess-1   → sessions view (find your thread there)
PHOENIX_HOST ?= http://localhost:6006
PHOENIX_PROJECT ?= cto
trace:
	@PID=$$(curl -sf "$(PHOENIX_HOST)/v1/projects" \
	  | $(PY) -c "import sys,json; \
d=json.load(sys.stdin); \
print(next((p['id'] for p in d.get('data',[]) if p['name']=='$(PHOENIX_PROJECT)'),''))"); \
	if [ -z "$$PID" ]; then open "$(PHOENIX_HOST)/projects"; \
	elif [ -n "$(S)" ]; then open "$(PHOENIX_HOST)/projects/$$PID/sessions"; \
	else open "$(PHOENIX_HOST)/projects/$$PID"; fi

# Phase 4: Semantic cache maintenance
cache-stats:
	$(PY) -c "import sys; sys.path.insert(0,'src'); \
from rag.memory.cache import _client, CACHE_COLLECTION; \
c=_client(); \
print('collection:', CACHE_COLLECTION, \
      'exists:', c.collection_exists(CACHE_COLLECTION), \
      'points:', c.count(CACHE_COLLECTION).count if c.collection_exists(CACHE_COLLECTION) else 0)"

cache-purge:
	$(PY) -c "import sys; sys.path.insert(0,'src'); \
from rag.memory.cache import cache_purge_expired; \
print('purged', cache_purge_expired(), 'expired entries')"

# Wipe all data
clean:
	docker compose down -v
	rm -rf data/qdrant data/postgres data/phoenix
	rm -f data/vocab.json

# ── Phase 10 — `cto` binary (PyInstaller, thin client) ──────────────
# Output: dist/cto-<os>-<arch>  (single executable, no Python on target)
# Distribution: offline / internal artifact share (no auto-update).
#
#   make binary               build for host arch (whatever you're on)
#   make binary-mac-arm64     build for macOS arm64 (requires arm Mac)
#   make binary-mac-x86_64    build for macOS x86_64 (requires Intel Mac)
#   make binary-clean         scrub build artifacts
#
# Each platform must be built on its native host — PyInstaller does
# not cross-compile. The make targets just enforce that the host
# matches the requested arch.

PYI := $(if $(wildcard .venv/bin/pyinstaller),.venv/bin/pyinstaller,pyinstaller)

binary:
	@$(PYI) --version >/dev/null 2>&1 || \
		(echo "install pyinstaller first: $(PY) -m pip install pyinstaller" && exit 1)
	@echo "→ building cto binary (host arch: $$(uname -m))"
	PYTHONPATH=src $(PYI) --clean --noconfirm packaging/pyinstaller_cto.spec
	@ls -lh dist/cto-* 2>/dev/null | sed 's/^/  /'

binary-mac-arm64:
	@[ "$$(uname)" = "Darwin" ] && [ "$$(uname -m)" = "arm64" ] || \
		(echo "binary-mac-arm64 requires macOS on Apple Silicon" && exit 1)
	@$(MAKE) binary

binary-mac-x86_64:
	@[ "$$(uname)" = "Darwin" ] && [ "$$(uname -m)" = "x86_64" ] || \
		(echo "binary-mac-x86_64 requires macOS on Intel" && exit 1)
	@$(MAKE) binary

binary-clean:
	rm -rf build/ dist/cto-* dist/cto __pycache__/*.spec.pkg
