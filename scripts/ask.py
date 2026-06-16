"""
CLI client for the LangGraph agent — invokes the graph directly (no HTTP).
Demonstrates multi-turn memory via --session, semantic cache via --no-cache,
and human-in-the-loop interrupt() via stdin prompt.

Usage:
  python scripts/ask.py "how does JWT validation work?"
  python scripts/ask.py "what file is that in?" --session abc123
  python scripts/ask.py "compare auth in mgmtapi vs enrollment" --user alice
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from rag.agents.graph import get_app
from rag.agents.interrupt_helpers import extract_interrupt, resume_command
from rag.memory.cache import cache_lookup, cache_store, store_eligible
from rag.tracing import trace_config, push_scores


def _print_event(event: dict, final: dict) -> None:
    for node, delta in event.items():
        if not isinstance(delta, dict):
            continue
        final.update(delta)
        if node == "query_analysis":
            qp = delta.get("query_plan") or {}
            print(f"  ▸ analysis: intent={qp.get('intent','?')} "
                  f"scope={qp.get('repo_scope') or 'ALL'}"
                  + (" needs_human" if qp.get("needs_human") else ""))
        elif node == "router":
            print(f"  ▸ router → {delta.get('route')}")
        elif node == "load_memories":
            mems = delta.get("user_memories") or []
            if mems:
                print(f"  ▸ loaded {len(mems)} user memor{'y' if len(mems)==1 else 'ies'}")
        elif node == "clarify":
            if delta.get("clarified"):
                qp = delta.get("query_plan") or {}
                print(f"  ▸ clarified → query={delta.get('query')!r} "
                      f"scope={qp.get('repo_scope') or '—'}")
        elif node == "deep_research":
            sq = delta.get("sub_questions") or []
            fd = delta.get("findings") or []
            print(f"  ▸ deep_research: {len(sq)} sub-questions → {len(fd)} parallel findings")
        elif node in ("agent_loop", "simple_rag"):
            for m in delta.get("messages", []):
                if isinstance(m, AIMessage) and m.tool_calls:
                    for tc in m.tool_calls:
                        print(f"  ▸ tool_call: {tc['name']}({tc['args']})")
                elif isinstance(m, ToolMessage):
                    preview = str(m.content).replace("\n", " ")[:100]
                    print(f"    ↳ result: {preview}...")
        elif node == "summarize":
            n = sum(1 for m in delta.get("messages", []) if type(m).__name__ == "RemoveMessage")
            if n:
                print(f"  ▸ summarized: compressed {n} old messages")


def _stream(app, payload, config, final: dict) -> dict | None:
    """Drive one stream pass; return interrupt request if paused, else None."""
    for event in app.stream(payload, config=config, stream_mode="updates"):
        if (req := extract_interrupt(event)):
            return req
        _print_event(event, final)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("question", help="Question to ask the agent")
    parser.add_argument("--session", default="cli-default", help="Session/thread ID for multi-turn")
    parser.add_argument("--user", default="anon", help="User ID for cross-session long-term memory")
    parser.add_argument("--prebuilt", action="store_true", help="Use create_react_agent instead of hand-rolled")
    parser.add_argument("--no-cache", action="store_true", help="Skip semantic cache lookup")
    vg = parser.add_mutually_exclusive_group()
    vg.add_argument("--terse", dest="verbosity", action="store_const", const="terse",
                    help="≤3-sentence direct answer")
    vg.add_argument("--detailed", dest="verbosity", action="store_const", const="detailed",
                    help="Comprehensive answer")
    parser.set_defaults(verbosity="normal")
    args = parser.parse_args()

    app = get_app(prebuilt=args.prebuilt or None)
    config, tracer = trace_config(
        args.session, user_id=args.user, query=args.question,
        tags=["cli", f"verbosity:{args.verbosity}"],
    )

    print(f"\n[session: {args.session}  user: {args.user}  verbosity: {args.verbosity}]\n")

    if not args.no_cache and args.verbosity in ("normal", "terse"):
        try:
            hit = cache_lookup(args.question, threshold=0.93)
        except Exception:
            hit = None
        if hit:
            print(f"  ▸ cache HIT (sim={hit['score']:.3f}, age={hit['age_sec']:.0f}s, "
                  f"q={hit['cached_query']!r})")
            print("\n" + "─" * 70)
            print(hit["answer"])
            print("─" * 70)
            if hit["citations"]:
                print("\nCitations:")
                for c in hit["citations"]:
                    print(f"  [{c.get('ref')}] {c.get('file')}")
            return

    inputs = {
        "query": args.question,
        "user_id": args.user,
        "verbosity": args.verbosity,
        "messages": [HumanMessage(content=args.question)],
    }

    final: dict = {}
    payload = inputs
    with tracer:
        while True:
            req = _stream(app, payload, config, final)
            if req is None:
                break
            # Human-in-the-loop: prompt and resume.
            print(f"\n  ❓ {req.get('question', '(clarification needed)')}")
            opts = req.get("options") or []
            if opts:
                for i, o in enumerate(opts, 1):
                    print(f"     {i}. {o}")
            try:
                ans = input("  > ").strip()
            except EOFError:
                ans = ""
            if opts and ans.isdigit() and 1 <= int(ans) <= len(opts):
                ans = opts[int(ans) - 1]
            payload = resume_command(ans)
            print()
        push_scores(tracer, final)
        trace_url = tracer.url()

    print(f"\n  ▸ iterations: {final.get('iteration', 0)}")
    print("\n" + "─" * 70)
    print(final.get("answer", "(no answer)"))
    print("─" * 70)

    citations = final.get("citations", [])
    if citations:
        print("\nCitations:")
        for c in citations:
            print(f"  [{c['ref']}] {c['file']}  {c.get('symbol', '')}")

    key, store_ok, why = store_eligible(args.question, final, args.verbosity)
    if not args.no_cache and store_ok and final.get("answer"):
        try:
            cache_store(key, final["answer"], citations)
            print(f"  ▸ cached under {key!r}")
        except Exception:
            pass
    elif final.get("answer"):
        print(f"  ▸ not cached ({why})")

    if trace_url:
        print(f"\n🔍 trace: {trace_url}")


if __name__ == "__main__":
    main()
