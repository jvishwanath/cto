"""
Per-turn state reset. Runs unconditionally as the first node so
checkpointed values from the previous turn don't leak forward —
independent of which feature flags are enabled.
"""

from ..state import AgentState


def reset_turn(state: AgentState) -> dict:
    return {
        "guard_flags": {},
        "eval_iter": 0,
        "eval_scores": {},
        "refine_hint": None,
        "pending_clarification": None,
        "clarified": {},
        "route": None,
        "retrieved_chunks": [],
        "citations": [],
        "sub_questions": [],
        "findings": [],
        "skill_name": None,
        "skill_cid": None,
        "skill_wt": None,
    }
