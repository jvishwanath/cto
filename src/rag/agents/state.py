"""
Shared state passed between LangGraph nodes.
Nodes return DELTAS (only changed keys); LangGraph merges via reducers.
"""

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    # Conversation history. add_messages reducer appends and dedups by id.
    messages: Annotated[list[BaseMessage], add_messages]

    # Original user question for this turn (set by API layer).
    query: str

    # Identity for long-term Store namespace (Phase 4-B). Defaults to "anon".
    user_id: str

    # Phase 9-C: scheduled runs have no user. When True, clarify
    # picks-all instead of interrupt(), and ask_user returns the
    # job's configured default (headless_answers[<question>])
    # or "skip".
    headless: bool
    headless_answers: dict

    # Response length: "terse" | "normal" | "detailed". Set by client
    # (--terse, ?verbosity=, UI toggle) or inline prefix ("q: …").
    verbosity: str
    # Cross-session facts loaded by load_memories at graph entry.
    user_memories: list[dict]

    # Pre-retrieval query analysis (Phase 4-F): structured scope/intent
    # extracted by the query_analysis node. Downstream nodes prefer this
    # over re-parsing the raw query.
    #   {"repo_scope": list[str], "intent": str, "search_queries": list[str],
    #    "needs_human": bool, "clarify_question": str|None}
    query_plan: dict

    # Guardrails (Phase 4.5): {"pii_redacted": [...], "blocked": str|None,
    # "secrets_scrubbed": int, "abstained": bool, "orphan_citations": [...]}
    guard_flags: dict

    # Evaluator / self-correction (Phase 4.5).
    eval_scores: dict        # {"retrieval": {...}, "answer": {...}}
    eval_iter: int           # retry counter
    refine_hint: str | None  # critique fed back into agent_loop on retry

    # Human-in-the-loop (Phase 4-E).
    pending_clarification: dict | None
    clarified: dict

    # Routing decision: "simple" | "agent" | "research" | "intro" |
    # "skill" | "blocked" | "superdev"
    route: str

    # Superdev only (wrapper pipeline): "read" | "action". Set by
    # superdev_loop from the tool-call mix of this turn. Read turns
    # are cacheable and graded; action turns skip both.
    turn_kind: str

    # Skill route (Phase 7). Set by router/slash; consumed by
    # skill_runner. cid is checkpointed so a persistent sandbox
    # container survives an ask_user interrupt() resume.
    skill_name: str | None
    skill_cid: str | None
    # Phase 8-D: serialized Worktree (repo, path, branch, src) so a
    # remediation skill's worktree survives an ask_user interrupt.
    skill_wt: dict | None

    # Chunks retrieved this turn. Last-write-wins so reset_turn can
    # clear it; the only fan-out lives in the deep_research subgraph
    # which has its own state.
    retrieved_chunks: list[dict]

    # Deep-research subgraph (Phase 4-C).
    sub_questions: list[dict]
    findings: list[dict]

    # Final answer text and structured citations.
    answer: str
    citations: list[dict]

    # Loop guard.
    iteration: int
