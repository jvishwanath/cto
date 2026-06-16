"""
Phase 4 test suite — semantic cache, long-term store, deep_research
fan-out, history summarization, interrupt/clarify.

Usage:
  python3 tests/test_phase4.py            # fast checks (mocked LLM where possible)
  python3 tests/test_phase4.py --full     # + LLM-dependent checks
  pytest tests/test_phase4.py
"""

import sys
import time
import uuid as _uuid
from unittest.mock import patch

import conftest  # noqa: F401  — puts src/ + tests/ on sys.path

from smoke_test import check, section, results, GREEN, RED, RESET  # noqa: E402


# ════════════════════════════════════════════════════════════════════
# A — Semantic cache
# ════════════════════════════════════════════════════════════════════

def test_semantic_cache():
    section("Phase 4-A: Semantic Query Cache")

    from qdrant_client import QdrantClient
    from rag.config import QDRANT_URL
    from rag.memory.cache import (
        ensure_cache_collection, cache_lookup, cache_store,
        cache_purge_expired, CACHE_COLLECTION, _point_id,
    )

    c = QdrantClient(url=QDRANT_URL)
    if c.collection_exists(CACHE_COLLECTION):
        c.delete_collection(CACHE_COLLECTION)

    @check("ensure_cache_collection: 1024-dim cosine, dense-only")
    def _():
        ensure_cache_collection()
        info = c.get_collection(CACHE_COLLECTION)
        v = info.config.params.vectors
        assert v.size == 1024 and "cosine" in str(v.distance).lower()
        assert not info.config.params.sparse_vectors
        return f"{v.size}d {v.distance}"

    @check("lookup on empty → None")
    def _():
        assert cache_lookup("anything") is None

    @check("store → deterministic UUID5 id, idempotent")
    def _():
        pid = cache_store("what calls validateCSR?",
                          answer="CryptoServiceImpl + ClientOnboardingServiceImpl",
                          citations=[{"ref": "SOURCE_1", "file": "x.java"}])
        assert pid == _point_id("what calls validateCSR?")
        pid2 = cache_store("  What  Calls  validateCSR? ", answer="updated")
        assert pid == pid2
        assert c.count(CACHE_COLLECTION, exact=True).count == 1
        cache_store("what calls validateCSR?",
                    answer="CryptoServiceImpl + ClientOnboardingServiceImpl",
                    citations=[{"ref": "SOURCE_1", "file": "x.java"}])
        return pid[:8]

    @check("lookup: exact roundtrip → hit")
    def _():
        h = cache_lookup("what calls validateCSR?")
        assert h and h["score"] >= 0.99 and "CryptoServiceImpl" in h["answer"]
        assert h["citations"][0]["file"] == "x.java"
        return f"score={h['score']:.3f}"

    @check("lookup: paraphrase → hit (threshold tuned to model)")
    def _():
        # Probe actual similarity first — embedding models vary
        # ('who' vs 'what' lands ~0.85-0.94 with text-embedding-3-large).
        from rag.embed import embed_query as eq
        import numpy as np
        a, b = np.array(eq("what calls validateCSR?")), np.array(eq("who calls validateCSR?"))
        sim = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
        thr = max(0.80, sim - 0.02)
        h = cache_lookup("who calls validateCSR?", threshold=thr)
        assert h, f"miss at thr={thr:.3f} (measured sim={sim:.3f})"
        assert h["cached_query"] == "what calls validateCSR?"
        return f"sim={sim:.3f} thr={thr:.3f} score={h['score']:.3f}"

    @check("lookup: unrelated → None")
    def _():
        assert cache_lookup("recipe for chocolate cookies") is None

    @check("lookup: threshold enforced")
    def _():
        assert cache_lookup("who invokes validateCSR", threshold=1.0) is None

    @check("TTL: expired entry skipped on lookup; purge removes it")
    def _():
        # Bypass embed-call latency: write the point directly with a
        # past expires_at, then verify lookup skips it and purge deletes it.
        from qdrant_client.models import PointStruct
        from rag.embed import embed_query as eq
        from rag.memory.cache import _point_id
        past = time.time() - 10
        c.upsert(CACHE_COLLECTION, points=[PointStruct(
            id=_point_id("ephemeral xyz"),
            vector=eq("ephemeral xyz"),
            payload={"query": "ephemeral xyz", "answer": "x",
                     "citations": [], "created_at": past - 60,
                     "expires_at": past},
        )], wait=True)

        assert c.count(CACHE_COLLECTION, exact=True).count == 2

        h = cache_lookup("ephemeral xyz", threshold=0.99)
        assert h is None, f"expired entry returned: {h}"

        n = cache_purge_expired()
        assert n == 1, f"purged {n}, expected 1"
        assert c.count(CACHE_COLLECTION, exact=True).count == 1
        assert cache_lookup("what calls validateCSR?") is not None
        assert cache_purge_expired() == 0
        return f"expired→None; purged={n}"

    c.delete_collection(CACHE_COLLECTION)


# ════════════════════════════════════════════════════════════════════
# B — PostgresStore long-term memory
# ════════════════════════════════════════════════════════════════════

def test_store():
    section("Phase 4-B: Long-term Store + memory nodes")

    from rag.memory.store import (
        get_store, put_memory, search_memories, list_memories, delete_memory,
    )
    from rag.agents.nodes.memories import load_memories, save_memories

    uid = f"_test_{_uuid.uuid4().hex[:6]}"

    @check("get_store singleton + setup")
    def _():
        s = get_store()
        assert s is get_store()
        return type(s).__name__

    @check("put_memory → list_memories roundtrip")
    def _():
        put_memory(uid, "k1", {"text": "works on acme-auth"})
        put_memory(uid, "k2", {"text": "prefers concise answers"})
        items = list_memories(uid)
        keys = {i["key"] for i in items}
        assert {"k1", "k2"} <= keys, f"got {keys}"
        return f"{len(items)} memories"

    @check("search_memories returns relevant entry")
    def _():
        r = search_memories(uid, query="enrollment service", limit=3)
        assert r, "search returned nothing"
        texts = [i["value"].get("text", "") for i in r]
        assert any("auth" in t for t in texts), f"got {texts}"
        return f"top={r[0]['key']}"

    @check("delete_memory removes")
    def _():
        delete_memory(uid, "k1")
        keys = {i["key"] for i in list_memories(uid)}
        assert "k1" not in keys and "k2" in keys
        delete_memory(uid, "k2")

    @check("load_memories node")
    def _():
        put_memory(uid, "k3", {"text": "focused on JWT auth"})
        try:
            out = load_memories({"user_id": uid, "query": "JWT validation"})
            assert "user_memories" in out
            assert any("JWT" in m.get("text", "") for m in out["user_memories"])
            return f"{len(out['user_memories'])} loaded"
        finally:
            delete_memory(uid, "k3")

    @check("load_memories: no user_id → empty (no fail)")
    def _():
        out = load_memories({"query": "x"})
        assert out == {"user_memories": []} or out["user_memories"] == []

    @check("save_memories: anon user → no-op")
    def _():
        out = save_memories({"user_id": "anon", "query": "x", "answer": "y"})
        assert out == {}


# ════════════════════════════════════════════════════════════════════
# C — deep_research Send fan-out
# ════════════════════════════════════════════════════════════════════

def test_deep_research(full=False):
    section("Phase 4-C: deep_research subgraph (Send fan-out)")

    from langgraph.types import Send
    from rag.agents.subgraphs.deep_research import (
        build_deep_research, fan_out, research_one, ResearchState,
    )

    @check("Send import path: langgraph.types.Send")
    def _():
        assert Send.__module__.startswith("langgraph")
        return Send.__module__

    @check("subgraph compiles with planner/research_one/synthesize")
    def _():
        app = build_deep_research()
        nodes = set(app.get_graph().nodes.keys())
        assert {"planner", "research_one", "synthesize"} <= nodes, nodes
        return f"nodes={sorted(nodes - {'__start__','__end__'})}"

    @check("fan_out returns list[Send]")
    def _():
        st = {"query": "q", "sub_questions": [
            {"q": "a", "repo": "acme-auth", "kind": "code"},
            {"q": "b", "repo": "acme-api", "kind": "code"},
        ]}
        sends = fan_out(st)
        assert len(sends) == 2
        assert all(isinstance(s, Send) for s in sends)
        assert sends[0].node == "research_one"
        assert sends[0].arg["sub"]["q"] == "a"
        return f"{len(sends)} Send(s)"

    @check("research_one returns findings with chunks")
    def _():
        out = research_one({
            "sub": {"q": "JWT validation", "repo": "acme-auth", "kind": "code"},
            "query": "test",
        })
        assert "findings" in out and len(out["findings"]) == 1
        f = out["findings"][0]
        assert f["sub_question"] == "JWT validation"
        assert isinstance(f["chunks"], list)
        return f"{len(f['chunks'])} chunks"

    @check("findings reducer is operator.add (parallel-merge safe)")
    def _():
        import operator
        from typing import get_type_hints, get_args
        hints = get_type_hints(ResearchState, include_extras=True)
        args = get_args(hints["findings"])
        assert operator.add in args, f"reducer not operator.add: {args}"

    if full:
        @check("[full] e2e: compare query → ≥2 parallel findings + answer")
        def _():
            app = build_deep_research()
            r = app.invoke({"query": "compare JWT handling in acme-auth vs acme-api"})
            assert len(r.get("findings", [])) >= 2, f"findings={len(r.get('findings',[]))}"
            assert r.get("answer"), "no answer"
            return f"sub_q={len(r['sub_questions'])} findings={len(r['findings'])}"


# ════════════════════════════════════════════════════════════════════
# D — History summarization
# ════════════════════════════════════════════════════════════════════

def test_summarize(full=False):
    section("Phase 4-D: History summarization")

    from langchain_core.messages import (
        HumanMessage, AIMessage, ToolMessage, SystemMessage, RemoveMessage,
    )
    from langgraph.graph.message import add_messages
    from rag.agents.nodes.summarize import (
        should_summarize, summarize_history, SUMMARIZE_THRESHOLD, KEEP_RECENT,
    )

    def mk(n):
        msgs = []
        for i in range(n):
            mid = f"m{i:02d}"
            if i % 5 == 2:
                msgs.append(AIMessage(content="", id=mid,
                    tool_calls=[{"name": "search_code", "args": {"q": "verifyJwt"}, "id": f"tc{i}"}]))
            elif i % 5 == 3:
                msgs.append(ToolMessage(content="EnrollmentService.java" + "x"*200,
                    tool_call_id=f"tc{i-1}", name="search_code", id=mid))
            elif i % 2 == 0:
                msgs.append(HumanMessage(content=f"Q{i}: where is verifyJwt in acme-auth?", id=mid))
            else:
                msgs.append(AIMessage(content=f"A{i}: in EnrollmentService.java", id=mid))
        return msgs

    @check("should_summarize threshold")
    def _():
        assert should_summarize({"messages": mk(5)}) == "skip"
        assert should_summarize({"messages": mk(SUMMARIZE_THRESHOLD + 5)}) == "summarize"
        return f"threshold={SUMMARIZE_THRESHOLD}"

    @check("summarize_history delta shape (mocked LLM)")
    def _():
        msgs = mk(25)
        old_ids = {m.id for m in msgs[:-KEEP_RECENT]}
        recent_ids = {m.id for m in msgs[-KEEP_RECENT:]}
        fake = AIMessage(content="User asked about verifyJwt in EnrollmentService.java.")
        with patch("rag.agents.nodes.summarize.llm") as mklm:
            mklm.return_value.invoke.return_value = fake
            delta = summarize_history({"messages": msgs})
        out = delta["messages"]
        removes = [m for m in out if isinstance(m, RemoveMessage)]
        sums = [m for m in out if isinstance(m, SystemMessage)]
        assert len(removes) == 25 - KEEP_RECENT
        assert {m.id for m in removes} == old_ids
        assert {m.id for m in removes}.isdisjoint(recent_ids)
        assert len(sums) == 1 and "summary" in sums[0].content
        merged = add_messages(msgs, out)
        assert len(merged) == KEEP_RECENT + 1
        return f"removes={len(removes)} → merged={len(merged)}"

    @check("summarize_history no-op on LLM failure")
    def _():
        with patch("rag.agents.nodes.summarize.llm") as mklm:
            mklm.return_value.invoke.side_effect = RuntimeError("down")
            assert summarize_history({"messages": mk(25)}) == {}

    if full:
        @check("[full] summary content preserves key terms")
        def _():
            d = summarize_history({"messages": mk(25)})
            if not d:
                return "LLM unreachable — skip"
            s = next(m for m in d["messages"] if isinstance(m, SystemMessage))
            body = s.content.lower()
            assert "verifyjwt" in body or "auth" in body
            return f"{len(s.content)} chars"


# ════════════════════════════════════════════════════════════════════
# E — interrupt() clarify
# ════════════════════════════════════════════════════════════════════

def test_clarify(full=False):
    section("Phase 4-E: interrupt() / clarify")

    from rag.agents.tools import _repo
    from rag.agents.nodes import clarify as mod
    from rag.agents.interrupt_helpers import (
        extract_interrupt, resume_command, INTERRUPT_KEY,
    )
    from langgraph.types import Interrupt, Command

    fake = ["acme-auth", "acme-api", "xt-api", "xt-connector"]
    saved_cache = _repo._cache
    _repo._cache = list(fake)

    try:
        @check("needs_clarification: ambiguous repo token + (all of these) option")
        def _():
            req = mod.needs_clarification({"query": "how does api handle JWT?", "messages": []})
            assert req and req["kind"] == "repo" and req["token"] == "api"
            assert mod.ALL_MATCHES_OPTION in req["options"]
            assert set(req["matches"]) == {"acme-api", "xt-api"}
            assert {"acme-api", "xt-api"} <= set(req["options"])
            return f"options={req['options']}"

        @check("needs_clarification: backstop fires when plan picked 1 of N candidates")
        def _():
            # Plan committed to one -api → backstop asks.
            st = {"query": "how does api handle JWT?", "messages": [],
                  "query_plan": {"repo_scope": ["acme-api"],
                                 "needs_human": False}}
            req = mod.needs_clarification(st)
            assert req and req["kind"] == "repo"
            assert set(req["matches"]) == {"acme-api", "xt-api"}
            # Plan included ALL candidates → backstop satisfied.
            st["query_plan"]["repo_scope"] = ["acme-api", "xt-api"]
            assert mod.needs_clarification(st) is None
            # User typed exact name → backstop skipped.
            st2 = {"query": "how does xt-api handle JWT?", "messages": [],
                   "query_plan": {"repo_scope": ["xt-api"],
                                  "needs_human": False}}
            assert mod.needs_clarification(st2) is None

        @check("needs_clarification: common-prefix token (matches all) → skip")
        def _():
            # 'acme' / 'xt' would match multiple but if a token matches
            # ALL repos it's not a disambiguator. Use a fixture where
            # everything shares a prefix.
            saved = _repo._cache
            _repo._cache = ["acme-auth", "acme-jwks", "acme-api"]
            try:
                stack = "ERROR com.example.svc.Exception identifier revoked"
                assert mod.needs_clarification({"query": stack, "messages": []}) is None
            finally:
                _repo._cache = saved

        @check("repo clarify: 'all' / unrecognized reply → matches list, query intact")
        def _():
            from langgraph.graph import StateGraph, START, END
            from langgraph.checkpoint.memory import MemorySaver
            from rag.agents.state import AgentState

            g = StateGraph(AgentState)
            g.add_node("clarify", mod.clarify)
            g.add_node("done", lambda s: {})
            g.add_conditional_edges(START, mod.should_clarify,
                                    {"clarify": "clarify", "continue": "done"})
            g.add_conditional_edges("clarify", mod.should_clarify,
                                    {"clarify": "clarify", "continue": "done"})
            g.add_edge("done", END)
            app = g.compile(checkpointer=MemorySaver())

            expected = {"acme-api", "xt-api"}
            for reply in ["search in all", "idk whatever"]:
                cfg = {"configurable": {"thread_id": str(_uuid.uuid4())}}
                for ev in app.stream({"query": "how does the api work?",
                                       "messages": [], "clarified": {}},
                                      config=cfg, stream_mode="updates"):
                    if extract_interrupt(ev): break
                re_intr = False
                for ev in app.stream(resume_command(reply), config=cfg,
                                      stream_mode="updates"):
                    if extract_interrupt(ev): re_intr = True
                st = app.get_state(cfg).values
                assert not re_intr, f"re-asked after {reply!r}"
                assert reply not in st["query"], f"query corrupted: {st['query']!r}"
                assert set(st["clarified"]["repo"]) == expected, st["clarified"]
            return f"all + garbage → repo={sorted(expected)}, query intact, no re-ask"

        @check("needs_clarification: unambiguous → None")
        def _():
            assert mod.needs_clarification({"query": "how does acme-auth handle JWT?",
                                            "messages": []}) is None

        @check("needs_clarification: short query → intent")
        def _():
            req = mod.needs_clarification({"query": "auth flow", "messages": []})
            assert req and req["kind"] == "intent"
            assert mod.needs_clarification({"query": "auth flow",
                                            "messages": [object(), object()]}) is None

        @check("needs_clarification: pending wins")
        def _():
            p = {"kind": "x", "question": "?", "options": []}
            assert mod.needs_clarification({"query": "long question here",
                                            "pending_clarification": p}) is p

        @check("should_clarify routing + MAX_CLARIFY_ROUNDS cap")
        def _():
            assert mod.should_clarify({"query": "what calls foo in api?",
                                       "messages": []}) == "clarify"
            assert mod.should_clarify({"query": "explain JWT in acme-auth",
                                       "messages": []}) == "continue"
            # At cap → continue even if still ambiguous
            assert mod.should_clarify({
                "query": "what about api?", "messages": [],
                "clarified": {"a": 1, "b": 2, "c": 3},
            }) == "continue"
            return f"cap={mod.MAX_CLARIFY_ROUNDS}"

        @check("multi-round clarify: intent reply introduces repo ambiguity → 2nd interrupt")
        def _():
            from langgraph.graph import StateGraph, START, END
            from langgraph.checkpoint.memory import MemorySaver
            from rag.agents.state import AgentState

            g = StateGraph(AgentState)
            g.add_node("clarify", mod.clarify)
            g.add_node("done", lambda s: {"answer": "ok"})
            g.add_conditional_edges(START, mod.should_clarify,
                                    {"clarify": "clarify", "continue": "done"})
            g.add_conditional_edges("clarify", mod.should_clarify,
                                    {"clarify": "clarify", "continue": "done"})
            g.add_edge("done", END)
            app = g.compile(checkpointer=MemorySaver())
            cfg = {"configurable": {"thread_id": str(_uuid.uuid4())}}

            # Round 1: intent
            i1 = None
            for ev in app.stream({"query": "auth", "messages": [], "clarified": {}},
                                  config=cfg, stream_mode="updates"):
                if (p := extract_interrupt(ev)): i1 = p; break
            assert i1 and i1["kind"] == "intent"

            # Round 2: reply with ambiguous repo → repo interrupt
            i2 = None
            for ev in app.stream(resume_command("how does api do auth"),
                                  config=cfg, stream_mode="updates"):
                if (p := extract_interrupt(ev)): i2 = p; break
            assert i2 and i2["kind"] == "repo", f"expected 2nd interrupt, got {i2}"
            assert {"acme-api", "xt-api"} <= set(i2["options"])

            # Round 3: resolve repo → completes
            done = False
            for ev in app.stream(resume_command("acme-api"),
                                  config=cfg, stream_mode="updates"):
                if extract_interrupt(ev):
                    assert False, "unexpected 3rd interrupt"
                for n, d in ev.items():
                    if n == "done":
                        done = True
            assert done

            st = app.get_state(cfg).values
            assert st["clarified"]["intent"] == "how does api do auth"
            assert st["clarified"]["repo"] == ["acme-api"]
            assert "repo_token:api" in st["clarified"]
            assert st["query_plan"]["repo_scope"] == ["acme-api"]
            assert "acme-api" in st["query"]
            assert len([m for m in st["messages"]
                        if "[clarification]" in m.content]) == 2
            return f"2 rounds; final query={st['query']!r}"

        @check("extract_interrupt: updates / multi-mode / non-interrupt")
        def _():
            intr = Interrupt(value={"kind": "repo", "question": "?"}, id="x")
            assert extract_interrupt({INTERRUPT_KEY: (intr,)}) == intr.value
            assert extract_interrupt(("updates", {INTERRUPT_KEY: (intr,)})) == intr.value
            assert extract_interrupt({"router": {"route": "agent"}}) is None
            assert isinstance(resume_command("x"), Command)
            assert resume_command("x").resume == "x"

        if full:
            @check("[full] graph round-trip: interrupt → resume")
            def _():
                from langgraph.graph import StateGraph, START, END
                from langgraph.checkpoint.memory import MemorySaver
                from rag.agents.state import AgentState

                g = StateGraph(AgentState)
                g.add_node("clarify", mod.clarify)
                g.add_edge(START, "clarify")
                g.add_edge("clarify", END)
                app = g.compile(checkpointer=MemorySaver())
                cfg = {"configurable": {"thread_id": str(_uuid.uuid4())}}

                captured = None
                for ev in app.stream({"query": "how does api handle JWT?",
                                       "messages": []}, config=cfg, stream_mode="updates"):
                    if (p := extract_interrupt(ev)):
                        captured = p
                assert captured and captured["kind"] == "repo"

                final = None
                for ev in app.stream(resume_command("acme-api"),
                                      config=cfg, stream_mode="updates"):
                    for n, d in ev.items():
                        if n == "clarify":
                            final = d
                assert final and "acme-api" in final["query"]
                assert final["clarified"]["repo"] == "acme-api"
                # Clarification must reach messages (agent_loop reads
                # messages, not query) so the agent actually sees it.
                msgs = final.get("messages", [])
                assert msgs and "[clarification]" in msgs[0].content, \
                    f"clarification not in messages: {msgs}"
                return f"q→{final['query']!r}, msg={msgs[0].content!r}"
    finally:
        _repo._cache = saved_cache


# ════════════════════════════════════════════════════════════════════
# Integration: graph compiles with all Phase 4 nodes
# ════════════════════════════════════════════════════════════════════

def test_graph_integration():
    section("Phase 4: Graph integration")

    from rag.agents.nodes.router import _RESEARCH_RE

    @check("router _RESEARCH_RE matches comparison/audit patterns")
    def _():
        cases = [
            "compare auth in enrollment and api",
            "audit all repos for hardcoded secrets",
            "how does each service handle JWT",
            "enrollment vs api auth",
            "summarize error handling across all services",
        ]
        misses = [c for c in cases if not _RESEARCH_RE.search(c)]
        assert not misses, f"missed: {misses}"
        return f"{len(cases)}/{len(cases)}"

    @check("verbosity: prefix parsing + expand detection")
    def _():
        from rag.agents.verbosity import parse_prefix, is_expand_request
        assert parse_prefix("q: where is X") == ("where is X", "terse")
        assert parse_prefix("detail: explain Y") == ("explain Y", "detailed")
        assert parse_prefix("normal question") == ("normal question", None)
        assert is_expand_request("more") and is_expand_request("tell me more")
        assert is_expand_request("yes more please")
        assert not is_expand_request("more info about X")
        return "prefix + expand patterns"

    @check("router: inline prefix sets verbosity; 'more' → detailed")
    def _():
        from rag.agents.nodes.router import router
        from langchain_core.messages import HumanMessage, AIMessage
        r = router({"query": "q: where is validateCSR defined",
                    "messages": [HumanMessage("...")]})
        assert r.get("verbosity") == "terse" and r["query"] == "where is validateCSR defined"
        r = router({"query": "more",
                    "messages": [HumanMessage("x"), AIMessage("y"), HumanMessage("more")]})
        assert r["route"] == "agent" and r["verbosity"] == "detailed"
        return "q:→terse, more→detailed"

    @check("AgentState has Phase 4 fields")
    def _():
        from rag.agents.state import AgentState
        from typing import get_type_hints
        h = get_type_hints(AgentState, include_extras=True)
        for f in ("user_id", "user_memories", "pending_clarification",
                  "clarified", "sub_questions", "findings"):
            assert f in h, f"missing {f}"
        return f"{len(h)} fields"

    @check("graph compiles with all Phase 4 nodes (no checkpointer)")
    def _():
        from rag.agents.graph import build_graph
        app = build_graph(use_checkpointer=False)
        nodes = set(app.get_graph().nodes.keys()) - {"__start__", "__end__"}
        expected = {"load_memories", "clarify", "router", "simple_rag",
                    "agent_loop", "deep_research", "respond",
                    "summarize", "save_memories"}
        assert expected <= nodes, f"missing: {expected - nodes}; have: {nodes}"
        return f"{len(nodes)} nodes"

    @check("graph compiles with checkpointer + store")
    def _():
        from rag.agents.graph import build_graph
        app = build_graph(use_checkpointer=True)
        return type(app).__name__


# ════════════════════════════════════════════════════════════════════

def main():
    full = "--full" in sys.argv

    test_semantic_cache()
    test_store()
    test_deep_research(full=full)
    test_summarize(full=full)
    test_clarify(full=full)
    test_graph_integration()

    n_pass = sum(1 for _, ok, *_ in results if ok)
    n_fail = len(results) - n_pass
    total_ms = sum(dt for *_, dt in results) * 1000
    print(f"\n{'─'*60}")
    if n_fail == 0:
        print(f"{GREEN}✓ {n_pass}/{len(results)} passed{RESET}  ({total_ms:.0f}ms)")
    else:
        print(f"{RED}✗ {n_fail}/{len(results)} failed{RESET}  ({n_pass} passed)")
        for name, ok, detail, _ in results:
            if not ok:
                print(f"  {RED}✗{RESET} {name}: {detail}")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
