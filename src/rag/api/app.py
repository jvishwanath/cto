"""
FastAPI service exposing the LangGraph agent.
POST /query — streams graph events as SSE; supports multi-turn via session_id.
Lifespan starts the filesystem watcher (gated by WATCHER_ENABLED).
"""

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

from ..agents.graph import get_app
from ..agents.interrupt_helpers import extract_interrupt, resume_command, INTERRUPT_KEY
from ..ingest.watcher import IngestWatcher
from ..ingest.incremental import list_repos
from ..ingest.docs import list_docs
from ..memory.cache import (
    cache_lookup, cache_store, ensure_cache_collection, store_eligible,
    seed_history_on_hit,
)
from ..tracing import init_tracing, trace_config, push_scores
from .auth import require_auth, auth_enabled

log = logging.getLogger(__name__)
WATCHER_ENABLED = os.environ.get("WATCHER_ENABLED", "true").lower() == "true"
CONFLUENCE_ENABLED = os.environ.get("CONFLUENCE_ENABLED", "true").lower() == "true"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s",
                        datefmt="%H:%M:%S")

    init_tracing()
    log.info("API auth: %s", "ENABLED" if auth_enabled() else
             "disabled (set CTO_API_KEYS to require Bearer)")

    # Reap any cto-skill-* containers orphaned by a prior crash.
    try:
        from ..skills.runner import reap_orphans
        from ..skills import load_skills
        n = len(reap_orphans())
        if n:
            log.info("reaped %d orphaned skill container(s)", n)
        sks = load_skills()
        log.info("skills loaded: %s", sorted(sks) or "(none)")
    except Exception:
        log.exception("skill startup failed")

    # Pre-warm the reranker model in the background so the first query
    # doesn't pay the ~4s CrossEncoder load cost.
    import threading
    from ..retrieval.reranker import _get_model
    threading.Thread(target=lambda: (_get_model(), log.info("reranker warmed")),
                     daemon=True, name="reranker-warmup").start()

    # Ensure the semantic cache collection exists.
    try:
        ensure_cache_collection()
    except Exception:
        log.exception("ensure_cache_collection failed")

    watcher = None
    if WATCHER_ENABLED:
        try:
            watcher = IngestWatcher().start()
            log.info("filesystem watcher started (set WATCHER_ENABLED=false to disable)")
        except Exception:
            log.exception("failed to start watcher; continuing without it")

    confluence_sched = None
    if CONFLUENCE_ENABLED:
        try:
            from ..connectors.scheduler import start_if_configured
            confluence_sched = start_if_configured()
            if confluence_sched:
                log.info(
                    "confluence scheduler started: %d space(s), every %.0fh "
                    "(set CONFLUENCE_ENABLED=false to disable)",
                    len(confluence_sched.cfg.spaces),
                    confluence_sched.cfg.sync_interval_hours,
                )
            else:
                log.info("confluence: no config at data/connectors/"
                         "confluence.yaml — scheduler not started")
        except Exception:
            log.exception("failed to start confluence scheduler")

    job_sched = None
    try:
        from ..scheduler import start_if_configured as start_jobs
        job_sched = start_jobs()
        if job_sched:
            log.info("job scheduler started: %d job(s) "
                     "(set CTO_SCHEDULER_ENABLED=false to disable)",
                     len(job_sched.jobs))
        else:
            log.info("job scheduler: no data/schedules.yaml — "
                     "not started")
    except Exception:
        log.exception("failed to start job scheduler")

    # Connect to any MCP servers configured in .mcp.json. App-wide
    # singleton pool — see docs/MCP.md. Fail-soft: a broken server
    # config doesn't sink the app.
    try:
        from ..mcp.client import connect_all as mcp_connect
        await mcp_connect()
    except Exception:
        log.exception("MCP connect_all failed; continuing without MCP")

    yield

    if watcher:
        watcher.stop()
    if confluence_sched:
        confluence_sched.stop()
    if job_sched:
        job_sched.stop()
    try:
        from ..mcp.client import shutdown_all as mcp_shutdown
        await mcp_shutdown()
    except Exception:
        log.exception("MCP shutdown_all failed")


api = FastAPI(title="CTO — Code/Trace/Origin", lifespan=lifespan)
graph = get_app()

# Mount Gradio chat UI at /ui (gated so tests/headless don't pull it in)
if os.environ.get("UI_ENABLED", "true").lower() == "true":
    try:
        import gradio as gr
        from .ui import build_ui
        ui_user = os.environ.get("CTO_UI_USER")
        ui_pass = os.environ.get("CTO_UI_PASSWORD")
        ui_auth = (ui_user, ui_pass) if ui_user and ui_pass else None
        gr.mount_gradio_app(api, build_ui(), path="/ui", auth=ui_auth,
                            auth_message="Code / Trace / Origin")
        log.info("Gradio UI mounted at /ui (auth: %s)",
                 "enabled" if ui_auth else "open")
    except Exception:
        log.exception("failed to mount Gradio UI; continuing without it")


@api.get("/")
def root():
    return {
        "service": "cto",
        "ui": "/ui",
        "api": {"query": "POST /query", "sources": "GET /sources",
                "sessions": "GET /sessions", "health": "GET /health",
                "openapi": "/docs"},
    }


@api.get("/sources")
def sources(_: str | None = Depends(require_auth)):
    return {"repos": list_repos(), "docs": list_docs()}


@api.get("/skills")
def skills(_: str | None = Depends(require_auth)):
    from ..skills import list_skills
    return {"skills": [
        {"name": s.name, "description": s.description,
         "mode": s.sandbox.mode, "max_iter": s.max_iter,
         "tools": list(s.tools_allowed),
         "triggers": list(s.triggers)}
        for s in list_skills()
    ]}


_JOBS_CSS = """
body{font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',
  sans-serif;background:#1a1a1a;color:#ddd;margin:0;padding:24px}
h1{font-weight:500;letter-spacing:.02em}
h1 .sub{color:#888;font-size:.6em;font-weight:400;margin-left:.5em}
table{border-collapse:collapse;width:100%;margin:12px 0 24px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #333;
  font-size:13px;white-space:nowrap}
th{color:#888;font-weight:500;text-transform:uppercase;
  letter-spacing:.05em;font-size:11px}
tr:hover td{background:#222}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;
  font-size:11px;font-weight:500}
.ok{background:#1a3a1a;color:#6c6}.error{background:#3a1a1a;color:#e88}
.running{background:#3a3a1a;color:#cc8}.never{color:#666}
.kind{font-family:ui-monospace,monospace;color:#888}
.dis{opacity:.4}
button{background:#2a2a2a;color:#ddd;border:1px solid #444;
  border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer}
button:hover{background:#333}
pre{background:#111;padding:12px;border-radius:8px;overflow-x:auto;
  font-size:12px;max-height:400px}
a{color:#7ab;text-decoration:none}a:hover{text-decoration:underline}
"""


def _jobs_html(rows: list[dict], runs: list[dict],
               log_tail: str) -> str:
    import html
    def td(r, k, cls=""):
        return f'<td class="{cls}">{html.escape(str(r.get(k,"—")))}</td>'
    def st(s):
        return f'<span class="badge {s}">{s}</span>'
    trs = "".join(
        f'<tr class="{"" if r["enabled"] else "dis"}">'
        f'<td><b>{html.escape(r["name"])}</b></td>'
        f'{td(r,"kind","kind")}{td(r,"schedule")}'
        f'{td(r,"repos")}{td(r,"last_run")}'
        f'<td>{st(r["status"])}</td>{td(r,"duration")}'
        f'{td(r,"next_fire")}<td>in {r["in"]}</td>'
        f'{td(r,"notify")}'
        f'<td><form method="post" action="/jobs/{r["name"]}/run" '
        f'style="display:inline">'
        f'<button type="submit">▶ fire</button></form></td></tr>'
        for r in rows)
    rtrs = "".join(
        f'<tr><td>{html.escape(r["job"])}</td>'
        f'<td>{html.escape(str(r.get("repo") or "—"))}</td>'
        f'<td>{r["started_at"]:%m-%d %H:%M:%S}</td>'
        f'<td>{st(r.get("status","—"))}</td>'
        f'<td>{html.escape((r.get("summary") or r.get("error") or "")[:120])}</td>'
        f'</tr>'
        for r in runs)
    return f"""<!doctype html><html><head>
<meta charset="utf-8"><title>CTO · Jobs</title>
<meta http-equiv="refresh" content="30">
<style>{_JOBS_CSS}</style></head><body>
<h1>⏰ Scheduled Jobs<span class="sub">data/schedules.yaml ·
  auto-refresh 30s · <a href="/jobs?format=json">json</a> ·
  <a href="/ui">ui</a></span></h1>
<table><thead><tr>
<th>name</th><th>kind</th><th>schedule</th><th>repos</th>
<th>last run</th><th>status</th><th>dur</th><th>next fire</th>
<th></th><th>notify</th><th></th>
</tr></thead><tbody>{trs or
  '<tr><td colspan=11 class=never>(no jobs — create '
  'data/schedules.yaml)</td></tr>'}</tbody></table>
<h2>Recent runs</h2>
<table><thead><tr><th>job</th><th>repo</th><th>started</th>
<th>status</th><th>summary / error</th></tr></thead>
<tbody>{rtrs or '<tr><td colspan=5 class=never>(none yet)</td></tr>'}
</tbody></table>
<h2>Log tail <span class="sub">data/logs/scheduler.log</span></h2>
<pre>{html.escape(log_tail or '(empty)')}</pre>
</body></html>"""


@api.get("/jobs")
def jobs(request: Request,
         format: str | None = None,
         _: str | None = Depends(require_auth)):
    from ..scheduler import status_rows, list_runs, LOG_PATH
    rows = status_rows()
    runs = list_runs(20)
    accept = request.headers.get("accept", "")
    if format == "json" or ("text/html" not in accept
                             and format != "html"):
        return {"jobs": rows, "runs": runs}
    tail = ""
    if LOG_PATH.exists():
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
        tail = "\n".join(lines[-120:])
    return HTMLResponse(_jobs_html(rows, runs, tail))


@api.post("/jobs/{name}/run")
def fire_job(name: str, bg: BackgroundTasks,
             request: Request,
             _: str | None = Depends(require_auth)):
    from ..scheduler import load_jobs, run_job_now, ensure_schema
    ensure_schema()
    j = next((x for x in load_jobs() if x.name == name), None)
    if j is None:
        return {"error": f"unknown job {name!r}"}
    bg.add_task(run_job_now, j)
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(
            f'<meta http-equiv="refresh" content="0;url=/jobs">'
            f'fired {name} — redirecting…')
    return {"fired": name, "kind": j.kind,
            "repos": list(j.repos)}


@api.get("/sessions")
def sessions(_: str | None = Depends(require_auth)):
    from ..indexing.graph_db import get_conn
    with get_conn().cursor() as cur:
        cur.execute(
            "SELECT thread_id, count(*) AS checkpoints FROM checkpoints "
            "GROUP BY thread_id ORDER BY max(checkpoint_id) DESC LIMIT 50"
        )
        return {"sessions": cur.fetchall()}


class QueryRequest(BaseModel):
    q: str | None = None
    session_id: str | None = None
    user_id: str = "anon"
    verbosity: str = "normal"  # "terse" | "normal" | "detailed"
    stream: bool = True
    use_cache: bool = True
    # If set, this request RESUMES a paused (interrupted) session with this answer.
    resume: str | None = None
    # Per-request tracing override (suppress_tracing() context).
    trace: bool = True


@api.get("/health")
def health():
    return {"status": "ok"}


@api.post("/query")
async def query(req: QueryRequest,
                principal: str | None = Depends(require_auth)):
    session_id = req.session_id or str(uuid.uuid4())
    # When identity comes from the proxy, it overrides the client-
    # supplied user_id (which is otherwise untrusted).
    user_id = (principal if principal and principal != "api-key"
               else req.user_id)
    config, tracer = trace_config(
        session_id, user_id=user_id, query=req.q,
        tags=["api", f"verbosity:{req.verbosity}"],
        suppress=not req.trace,
    )

    # Resume path: client is answering an interrupt() — no new query.
    if req.resume is not None:
        payload = resume_command(req.resume)
    else:
        if not req.q:
            return {"error": "q is required (or resume to answer an interrupt)"}
        from ..agents.nodes.intro import is_intro_query
        if (req.use_cache and req.verbosity in ("normal", "terse")
                and not is_intro_query(req.q)):
            try:
                hit = cache_lookup(req.q, threshold=0.93)
            except Exception:
                hit = None
            if hit:
                seed_history_on_hit(graph, config, req.q, hit)
                body = {
                    "session_id": session_id, "cached": True,
                    "cache_score": hit["score"], "cached_query": hit["cached_query"],
                    "answer": hit["answer"], "citations": hit["citations"],
                    "route": "cache", "iterations": 0,
                }
                if not req.stream:
                    return body
                async def cached_stream():
                    yield {"event": "session", "data": json.dumps({"session_id": session_id})}
                    yield {"event": "cache", "data": json.dumps(
                        {"score": hit["score"], "cached_query": hit["cached_query"]})}
                    yield {"event": "done", "data": json.dumps(body)}
                return EventSourceResponse(cached_stream())

        payload = {
            "query": req.q,
            "user_id": user_id,
            "verbosity": req.verbosity if req.verbosity in ("terse", "normal", "detailed") else "normal",
            "messages": [HumanMessage(content=req.q)],
        }

    if not req.stream:
        with tracer:
            result = graph.invoke(payload, config=config)
            push_scores(tracer, result)
        # Detect interrupt in non-streaming mode.
        st = graph.get_state(config)
        if getattr(st, "tasks", None):
            for t in st.tasks:
                if getattr(t, "interrupts", None):
                    intr = t.interrupts[0]
                    return {"session_id": session_id, "interrupt": intr.value}
        key, store_ok, _ = store_eligible(
            req.q or "", result, req.verbosity,
            was_resume=req.resume is not None)
        if req.use_cache and store_ok and result.get("answer"):
            try:
                cache_store(key, result["answer"], result.get("citations"))
            except Exception:
                pass
        return {
            "session_id": session_id,
            "route": result.get("route"),
            "answer": result.get("answer", ""),
            "citations": result.get("citations", []),
            "iterations": result.get("iteration", 0),
            "trace_url": tracer.url(),
        }

    async def event_stream():
        with tracer:
            async for ev in _event_stream_inner():
                yield ev

    async def _event_stream_inner():
        yield {"event": "session", "data": json.dumps({"session_id": session_id})}

        final_state = {}
        interrupted = None
        # Loop var named `kind` to avoid shadowing a future `mode`
        # parameter (parity with cli.py/ui.py).
        for kind, event in graph.stream(
                payload, config=config,
                stream_mode=["updates", "custom"]):
            if kind == "custom":
                se = (event or {}).get("skill_event")
                if se:
                    yield {"event": "skill", "data": json.dumps(se)}
                continue
            if (ireq := extract_interrupt(event)):
                interrupted = ireq
                yield {"event": "interrupt", "data": json.dumps(ireq)}
                break

            for node_name, delta in event.items():
                if not isinstance(delta, dict):
                    continue
                final_state.update(delta)
                out = {"node": node_name}

                if node_name == "router":
                    out["route"] = delta.get("route")
                if node_name == "query_analysis":
                    qp = delta.get("query_plan") or {}
                    out["intent"] = qp.get("intent")
                    out["repo_scope"] = qp.get("repo_scope") or []
                    out["needs_human"] = bool(qp.get("needs_human"))
                if node_name == "input_guard" and delta.get("query"):
                    out["resolved_query"] = delta["query"]
                if node_name == "clarify" and delta.get("clarified"):
                    qp = delta.get("query_plan") or {}
                    out["resolved_query"] = delta.get("query")
                    out["repo_scope"] = qp.get("repo_scope") or []
                if node_name == "load_memories":
                    out["n_memories"] = len(delta.get("user_memories") or [])
                if node_name == "evaluator":
                    es = delta.get("eval_scores") or {}
                    out["eval"] = es.get("answer") or {}
                    out["refine_hint"] = delta.get("refine_hint")
                    out["eval_iter"] = delta.get("eval_iter")
                if node_name == "deep_research":
                    out["sub_questions"] = len(delta.get("sub_questions") or [])
                    out["findings"] = len(delta.get("findings") or [])
                if "messages" in delta:
                    for m in delta["messages"]:
                        if isinstance(m, AIMessage) and m.tool_calls:
                            out.setdefault("tool_calls", []).extend(
                                {"name": tc["name"], "args": tc["args"]} for tc in m.tool_calls
                            )
                        elif isinstance(m, ToolMessage):
                            out.setdefault("tool_results", []).append(
                                {"tool_call_id": m.tool_call_id, "preview": str(m.content)[:300]}
                            )
                if "answer" in delta:
                    out["answer"] = delta["answer"]

                yield {"event": "node", "data": json.dumps(out)}

        if interrupted:
            yield {"event": "paused", "data": json.dumps(
                {"session_id": session_id, "interrupt": interrupted})}
            return

        key, store_ok, _ = store_eligible(
            req.q or "", final_state, req.verbosity,
            was_resume=req.resume is not None)
        if req.use_cache and store_ok and final_state.get("answer"):
            try:
                cache_store(key, final_state["answer"],
                            final_state.get("citations"))
            except Exception:
                pass

        yield {
            "event": "done",
            "data": json.dumps({
                "session_id": session_id,
                "route": final_state.get("route"),
                "answer": final_state.get("answer", ""),
                "citations": final_state.get("citations", []),
                "iterations": final_state.get("iteration", 0),
                "trace_url": tracer.url(),
            }),
        }

    return EventSourceResponse(event_stream())
