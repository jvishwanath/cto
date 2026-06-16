"""
Latency benchmark: 10 scenarios spanning every route. Times each
graph node by stamping stream(..., stream_mode="updates") events,
so we see WHERE the time goes (analysis LLM? retrieval? rerank?
agent loop? evaluator?). One warm-up run first to amortize reranker
load + first-LLM-connection cost.

Usage:
  make bench
  python scripts/bench_latency.py --no-cache --skip research
"""

import argparse
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from langchain_core.messages import HumanMessage  # noqa: E402
from rag.agents.graph import get_app  # noqa: E402
from rag.agents.interrupt_helpers import extract_interrupt  # noqa: E402
from rag.agents.tools._repo import available_repos  # noqa: E402
from rag.memory.cache import cache_lookup, cache_store  # noqa: E402

# Build scenarios from the LIVE inventory so queries hit real data.
_repos = available_repos(refresh=True)
_R1 = _repos[0] if _repos else "acme-auth"
_R2 = _repos[1] if len(_repos) > 1 else _R1


SCENARIOS: list[dict] = [
    {"id": "S0", "name": "warmup (discarded)",
     "q": f"q: what is {_R1}", "warmup": True},

    {"id": "S1", "name": "intro route (no LLM/retrieval)",
     "q": "what can i do here?", "expect_route": "intro"},

    {"id": "S2", "name": "meta inventory → agent(repo_info)",
     "q": "what repos are indexed here?", "expect_route": "agent"},

    {"id": "S3", "name": "simple route — single retrieve+generate",
     "q": f"q: explain what {_R1} does at a high level",
     "expect_route": "simple"},

    {"id": "S4", "name": "agent — structural (code graph callers)",
     "q": "who calls validateCSR?", "expect_route": "agent"},

    {"id": "S5", "name": "agent — definition lookup",
     "q": "where is validateCSR defined?", "expect_route": "agent"},

    {"id": "S6", "name": "agent — docs/confluence cascade",
     "q": "find the enrollment runbook from docs",
     "expect_route": "agent"},

    {"id": "S7", "name": "agent — git history tool",
     "q": f"show me the last 3 commits in {_R1}",
     "expect_route": "agent"},

    {"id": "S8", "name": "clarify interrupt (ambiguous repo)",
     "q": "explain auth in mgmtapi", "expect_interrupt": True},

    {"id": "S9", "name": "research — compare 2 repos (fan-out)",
     "q": f"compare JWT handling in {_R1} vs {_R2}",
     "expect_route": "research"},

    {"id": "S10", "name": "cache hit (re-ask S5)",
     "q": "where is validateCSR defined?", "cache_only": True},
]


def _run_one(graph, sc: dict, *, no_cache: bool) -> dict:
    """Returns {total, route, nodes:[(name,dt,meta)], interrupted,
    n_tools, cached}."""
    q = sc["q"]
    sid = f"bench-{sc['id']}-{uuid.uuid4().hex[:6]}"
    cfg = {"configurable": {"thread_id": sid}}

    t_start = time.perf_counter()

    if sc.get("cache_only"):
        hit = cache_lookup(q, threshold=0.93)
        dt = time.perf_counter() - t_start
        return {"total": dt, "route": "cache",
                "nodes": [("cache_lookup", dt,
                           f"hit={bool(hit)} sim="
                           f"{hit['score']:.2f}" if hit else "MISS")],
                "interrupted": False, "n_tools": 0,
                "cached": bool(hit)}

    nodes: list[tuple[str, float, str]] = []
    last = t_start
    route = None
    n_tools = 0
    interrupted = False

    payload = {"query": q, "user_id": "bench", "verbosity": "normal",
               "messages": [HumanMessage(content=q)]}

    for ev in graph.stream(payload, config=cfg, stream_mode="updates"):
        now = time.perf_counter()
        if extract_interrupt(ev):
            nodes.append(("⟲ interrupt", now - last, ""))
            interrupted = True
            break
        for name, delta in ev.items():
            if not isinstance(delta, dict):
                nodes.append((name, now - last, "—"))
                last = now
                continue
            meta = ""
            if name == "router":
                route = delta.get("route")
                meta = f"→ {route}"
            elif name == "query_analysis":
                qp = delta.get("query_plan") or {}
                meta = (f"intent={qp.get('intent')} "
                        f"nh={qp.get('needs_human')}")
            elif name == "evaluator":
                meta = (f"iter={delta.get('eval_iter')} "
                        f"hint={'Y' if delta.get('refine_hint') else 'N'}")
            elif "messages" in delta:
                tc = sum(len(m.tool_calls) for m in delta["messages"]
                         if hasattr(m, "tool_calls") and m.tool_calls)
                if tc:
                    n_tools += tc
                    meta = f"{tc} tool(s)"
            nodes.append((name, now - last, meta))
            last = now

    total = time.perf_counter() - t_start

    # Seed cache for S10.
    if not no_cache and not interrupted and not sc.get("warmup"):
        try:
            final = graph.get_state(cfg).values
            ans = final.get("answer")
            if ans and final.get("route") not in (None, "blocked", "intro"):
                cache_store(final.get("query") or q, ans,
                            final.get("citations"))
        except Exception:
            pass

    return {"total": total, "route": route, "nodes": nodes,
            "interrupted": interrupted, "n_tools": n_tools,
            "cached": False}


def _fmt_secs(s: float) -> str:
    return f"{s*1000:6.0f} ms" if s < 1 else f"{s:6.2f} s "


def _bar(frac: float, width: int = 24) -> str:
    n = max(0, min(width, round(frac * width)))
    return "█" * n + "·" * (width - n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--skip", nargs="*", default=[],
                    help="scenario ids or 'research'/'clarify' to skip")
    ap.add_argument("--detail", action="store_true",
                    help="print per-node breakdown for every scenario")
    ap.add_argument("--trace", action="store_true",
                    help="enable Phoenix tracing (measure overhead)")
    args = ap.parse_args()

    import os
    os.environ["PHOENIX_ENABLED"] = "true" if args.trace else "false"
    os.environ.setdefault("UI_ENABLED", "false")

    if args.trace:
        from rag.tracing import init_tracing
        ok = init_tracing(quiet=True)
        print(f"tracing: {'ON' if ok else 'init failed → OFF'}")
    else:
        print("tracing: OFF")
    print(f"repos: {_repos}\n")
    graph = get_app()

    results: list[tuple[dict, dict]] = []
    for sc in SCENARIOS:
        if sc["id"] in args.skip:
            continue
        if "research" in args.skip and sc.get("expect_route") == "research":
            continue
        if "clarify" in args.skip and sc.get("expect_interrupt"):
            continue
        label = f"[{sc['id']}] {sc['name']}"
        print(f"▸ {label:<48} {sc['q']!r}")
        try:
            r = _run_one(graph, sc, no_cache=args.no_cache)
        except Exception as e:
            print(f"  ✗ {type(e).__name__}: {e}\n")
            continue
        if sc.get("warmup"):
            print(f"  (warmup: {_fmt_secs(r['total'])} — discarded)\n")
            continue
        results.append((sc, r))
        flag = ("⟲ interrupt" if r["interrupted"]
                else f"route={r['route']}  tools={r['n_tools']}")
        print(f"  ⏱ {_fmt_secs(r['total'])}  {flag}")
        if args.detail:
            for name, dt, meta in r["nodes"]:
                print(f"      {_fmt_secs(dt)}  "
                      f"{_bar(dt / max(r['total'], 1e-6))}  "
                      f"{name:<16} {meta}")
        print()

    # ── Ranked summary ──────────────────────────────────────────────
    results.sort(key=lambda x: x[1]["total"])
    tmax = max((r["total"] for _, r in results), default=1.0)

    print("=" * 86)
    print(f"{'rank':<5}{'id':<5}{'scenario':<40}{'route':<10}"
          f"{'tools':>6}  {'total':>9}   relative")
    print("-" * 86)
    for i, (sc, r) in enumerate(results, 1):
        rt = ("interrupt" if r["interrupted"]
              else (r["route"] or "—"))
        print(f"{i:<5}{sc['id']:<5}{sc['name'][:38]:<40}{rt:<10}"
              f"{r['n_tools']:>6}  {_fmt_secs(r['total'])}  "
              f"{_bar(r['total'] / tmax)}")
    print("=" * 86)

    # ── Per-node aggregate (where does time go overall?) ────────────
    agg: dict[str, list[float]] = {}
    for _, r in results:
        for name, dt, _ in r["nodes"]:
            agg.setdefault(name, []).append(dt)
    print(f"\n{'node':<18}{'calls':>6}{'total':>10}{'mean':>10}"
          f"{'max':>10}")
    print("-" * 54)
    for name, xs in sorted(agg.items(), key=lambda kv: -sum(kv[1])):
        print(f"{name:<18}{len(xs):>6}{_fmt_secs(sum(xs)):>10}"
              f"{_fmt_secs(sum(xs)/len(xs)):>10}"
              f"{_fmt_secs(max(xs)):>10}")

    # ── Hotspot per scenario (top-2 nodes) ──────────────────────────
    print(f"\n{'id':<5}{'top time sinks (node: secs)':<60}")
    print("-" * 65)
    for sc, r in sorted(results, key=lambda x: -x[1]["total"]):
        top = sorted(r["nodes"], key=lambda n: -n[1])[:2]
        sinks = "  ·  ".join(f"{n}: {_fmt_secs(dt).strip()}"
                              for n, dt, _ in top)
        print(f"{sc['id']:<5}{sinks}")


if __name__ == "__main__":
    main()
