"""
StateGraph assembly (Phase 4 / 4.5):

  START → reset_turn → input_guard
                            │
                  blocked ──┼── continue
                     │      ▼
                     │  load_memories → query_analysis → [clarify?] → router
                     │                                                  ├─ simple   → simple_rag ───┐
                     │                                                  ├─ agent    → agent_loop ───┤
                     │                                                  └─ research → deep_research ┤
                     │                                                                              ▼
                     │                                                                          evaluator
                     │                                                                      retry ◀─┤ (≤2)
                     │                                                                              │ pass
                     └──────────────────────────────────────────────────────────────────────────────▼
                                                                                            output_guard
                                                                                                 │
                                                                                                 ▼
                                                                                              respond
                                                                                                 │
                                                                                    [summarize?] │
                                                                                                 ▼
                                                                                         save_memories → END

reset_turn always runs first (clears checkpointed per-turn fields).
input_guard runs before any LLM call so PII redaction and
jailbreak/off-topic rejection precede query_analysis and clarify.

Compiled with PostgresSaver checkpointer (per-thread) + PostgresStore
(cross-session). Phase-4 features can be disabled via env flags so the
Phase-2 behavior is recoverable for comparison.
"""

import logging
import os

from langgraph.graph import StateGraph, START, END

from .state import AgentState
from .nodes.router import router, route_decision
from .nodes.intro import intro
from .nodes.simple_rag import simple_rag
from ..skills.runner import skill_runner
from .nodes.agent_loop import agent_loop_handrolled, agent_loop_prebuilt
from .nodes.respond import respond
from .nodes.reset import reset_turn
from .nodes.clarify import clarify, should_clarify
from .nodes.query_analysis import query_analysis
from .nodes.guards import input_guard, input_guard_route, output_guard
from .nodes.evaluator import evaluator, should_retry
from .nodes.summarize import summarize_history, should_summarize
from .nodes.memories import load_memories, save_memories
from .subgraphs.deep_research import get_deep_research
from ..memory.checkpointer import get_checkpointer

log = logging.getLogger(__name__)

# Feature flags — default ON; flip off for A/B against Phase 2 behavior.
ENABLE_MEMORIES = os.environ.get("ENABLE_MEMORIES", "true").lower() == "true"
ENABLE_CLARIFY = os.environ.get("ENABLE_CLARIFY", "true").lower() == "true"
ENABLE_QUERY_ANALYSIS = os.environ.get("ENABLE_QUERY_ANALYSIS", "true").lower() == "true"
ENABLE_RESEARCH = os.environ.get("ENABLE_RESEARCH", "true").lower() == "true"
ENABLE_SUMMARIZE = os.environ.get("ENABLE_SUMMARIZE", "true").lower() == "true"
ENABLE_GUARDS = os.environ.get("ENABLE_GUARDS", "true").lower() == "true"
ENABLE_EVALUATOR = os.environ.get("ENABLE_EVALUATOR", "true").lower() == "true"


def deep_research_node(state: AgentState) -> dict:
    """Wrapper: invoke the deep_research subgraph and lift its output
    into the parent AgentState shape."""
    from langchain_core.messages import AIMessage

    sub = get_deep_research()
    result = sub.invoke({
        "query": state["query"],
        "verbosity": state.get("verbosity") or "normal",
        "query_plan": state.get("query_plan") or {},
    })
    answer = result.get("answer", "")
    return {
        # CRITICAL: append the answer to messages so the next turn's
        # agent_loop sees this question was answered. Without this,
        # consecutive research-routed turns leave orphaned HumanMessages
        # and the agent tries to answer all of them at once.
        "messages": [AIMessage(content=answer)] if answer else [],
        "answer": answer,
        "citations": result.get("citations", []),
        "sub_questions": result.get("sub_questions", []),
        "findings": result.get("findings", []),
        "iteration": len(result.get("findings", [])),
    }


def build_graph(use_checkpointer: bool = True, prebuilt: bool | None = None):
    if prebuilt is None:
        prebuilt = os.environ.get("AGENT_PREBUILT", "false").lower() == "true"

    g = StateGraph(AgentState)

    # ── Nodes ────────────────────────────────────────────────────────
    g.add_node("reset_turn", reset_turn)
    g.add_node("input_guard", input_guard)
    g.add_node("load_memories", load_memories)
    g.add_node("query_analysis", query_analysis)
    g.add_node("clarify", clarify)
    g.add_node("router", router)
    g.add_node("intro", intro)
    g.add_node("skill_runner", skill_runner)
    g.add_node("simple_rag", simple_rag)
    g.add_node("agent_loop", agent_loop_prebuilt if prebuilt else agent_loop_handrolled)
    g.add_node("deep_research", deep_research_node)
    g.add_node("evaluator", evaluator)
    g.add_node("output_guard", output_guard)
    g.add_node("respond", respond)
    g.add_node("summarize", summarize_history)
    g.add_node("save_memories", save_memories)

    # Targets that depend on which Phase-4.5 features are enabled.
    guard_tail = "output_guard" if ENABLE_GUARDS else "respond"
    eval_entry = "evaluator" if ENABLE_EVALUATOR else guard_tail

    # ── Edges ────────────────────────────────────────────────────────
    # reset_turn ALWAYS runs first — clears per-turn checkpoint state
    # regardless of feature flags.
    g.add_edge(START, "reset_turn")

    # Build the post-guard pipeline as an ordered list of enabled
    # stages, then chain them. The guard's "continue" branch (or
    # reset_turn if guards are off) feeds the first stage; the last
    # stage hosts the should_clarify conditional → router.
    stages: list[str] = []
    if ENABLE_MEMORIES:
        stages.append("load_memories")
    if ENABLE_QUERY_ANALYSIS:
        stages.append("query_analysis")
    for a, b in zip(stages, stages[1:]):
        g.add_edge(a, b)

    clarify_enabled = ENABLE_CLARIFY and use_checkpointer

    # Decide what comes after the last pre-router stage. If clarify is
    # on, the should_clarify gate sits there; otherwise straight edge.
    def _attach_to_router(src: str) -> None:
        if clarify_enabled:
            g.add_conditional_edges(
                src, should_clarify,
                {"clarify": "clarify", "continue": "router"},
            )
        else:
            g.add_edge(src, "router")

    # Input guard: redact PII, block jailbreak/off-topic BEFORE any
    # LLM call or human interrupt. Blocked short-circuits to output.
    if ENABLE_GUARDS:
        g.add_edge("reset_turn", "input_guard")
        if stages:
            g.add_conditional_edges(
                "input_guard", input_guard_route,
                {"continue": stages[0], "blocked": guard_tail},
            )
            _attach_to_router(stages[-1])
        else:
            # No intermediate stages: fold the clarify gate into the
            # guard's continue branch.
            def _guard_then_clarify(s):
                if input_guard_route(s) == "blocked":
                    return "blocked"
                return should_clarify(s) if clarify_enabled else "continue"
            g.add_conditional_edges(
                "input_guard", _guard_then_clarify,
                {"blocked": guard_tail, "clarify": "clarify",
                 "continue": "router"},
            )
    else:
        if stages:
            g.add_edge("reset_turn", stages[0])
            _attach_to_router(stages[-1])
        else:
            _attach_to_router("reset_turn")

    if clarify_enabled:
        # After a clarify round, re-run analysis on the enriched query
        # so the plan reflects the user's answer, then re-gate.
        if ENABLE_QUERY_ANALYSIS:
            g.add_edge("clarify", "query_analysis")
        else:
            g.add_conditional_edges(
                "clarify", should_clarify,
                {"clarify": "clarify", "continue": "router"},
            )

    # Router → 5 routes.
    route_map = {"simple": "simple_rag", "agent": "agent_loop",
                 "intro": "intro", "skill": "skill_runner"}
    if ENABLE_RESEARCH:
        route_map["research"] = "deep_research"
    else:
        route_map["research"] = "agent_loop"  # fall back
    g.add_conditional_edges("router", route_decision, route_map)
    # Intro and skill skip evaluator/output_guard — intro has no
    # retrieval; skill's deliverable IS the report (write_report).
    g.add_edge("intro", "respond")
    g.add_edge("skill_runner", "respond")

    # Answer-producing nodes feed the evaluator (or guard/respond if
    # evaluator disabled).
    g.add_edge("simple_rag", eval_entry)
    g.add_edge("agent_loop", eval_entry)
    g.add_edge("deep_research", eval_entry)

    # Evaluator: grade retrieval + answer; loop back to agent_loop on
    # low score (capped at MAX_EVAL_ITER inside should_retry).
    if ENABLE_EVALUATOR:
        g.add_conditional_edges(
            "evaluator", should_retry,
            {"retry": "agent_loop", "pass": guard_tail},
        )

    # Output guard: scrub secrets, verify citations, abstain on weak
    # retrieval — then hand to respond.
    if ENABLE_GUARDS:
        g.add_edge("output_guard", "respond")

    # After respond: optionally summarize long history, then save memories.
    tail = "save_memories" if ENABLE_MEMORIES else END
    if ENABLE_SUMMARIZE:
        g.add_conditional_edges(
            "respond", should_summarize,
            {"summarize": "summarize", "skip": tail},
        )
        g.add_edge("summarize", tail)
    else:
        g.add_edge("respond", tail)

    if ENABLE_MEMORIES:
        g.add_edge("save_memories", END)

    # ── Compile ──────────────────────────────────────────────────────
    checkpointer = get_checkpointer() if use_checkpointer else None
    store = None
    if ENABLE_MEMORIES:
        try:
            from ..memory.store import get_store
            store = get_store()
        except Exception as e:
            log.warning("PostgresStore unavailable (%s); long-term memory disabled", e)

    return g.compile(checkpointer=checkpointer, store=store)


def get_app(prebuilt: bool | None = None):
    return build_graph(use_checkpointer=True, prebuilt=prebuilt)
