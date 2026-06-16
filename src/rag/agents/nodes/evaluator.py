"""
Evaluator / self-correction loop (Phase 4.5).

Two graders, one node:

  grade_retrieval (CRAG) — score each retrieved chunk 0/1 relevant to
      the question. If fewer than MIN_RELEVANT survive, retrieval was
      weak: emit a refine_hint and loop back to agent_loop so it can
      try different tools (grep, repo_info, web_search).

  grade_answer — LLM-as-judge on groundedness (every claim supported
      by a cited chunk?) and completeness (does it answer the
      query_plan.intent?). If combined < THRESHOLD and we haven't
      retried MAX_EVAL_ITER times, loop back with a critique.

Both use the cheap router model with structured output. Skipped on
the 'simple' route (single-shot, not worth the latency).
"""

import logging
import re

from pydantic import BaseModel, Field
from langchain_core.messages import (
    HumanMessage, SystemMessage, ToolMessage,
)

from ..llm import llm
from ..state import AgentState

log = logging.getLogger(__name__)

MAX_EVAL_ITER = 2
ANSWER_THRESHOLD = 0.6
MIN_RELEVANT = 2
MAX_GRADED_CHUNKS = 8

# Cheap pre-check: agent gave up without finding anything. Triggers
# a retry with a docs/cascade hint, no LLM grading needed.
_EMPTY_ANSWER_RE = re.compile(
    r"\bno\b[^.]{0,60}?\b(?:were|was|are|is)?\s*found\b"
    r"|\bnot\s+found\b"
    r"|\bcould(?:n't|\s+not)\s+find\b"
    r"|\bno\s+(?:match(?:es|ing)?|results?|files?|documentation|"
    r"runbooks?|docs?|pages?)\b"
    r"|\bnothing\s+(?:matched|found|relevant)\b"
    r"|\bmay\s+live\s+(?:outside|elsewhere|in\b)"
    r"|\bno\s+indexed\b"
    r"|\bdoes(?:n't|\s+not)\s+(?:exist|appear)\b"
    r"|\bunable\s+to\s+(?:find|locate)\b",
    re.IGNORECASE,
)


class ChunkGrade(BaseModel):
    relevant: list[int] = Field(
        description=(
            "Indices (0-based) of context chunks that are RELEVANT to "
            "answering the question. A chunk is relevant if it contains "
            "information that would appear in or directly support a "
            "correct answer. Omit boilerplate, build files, and "
            "tangential matches."
        ),
    )


class AnswerGrade(BaseModel):
    groundedness: float = Field(
        ge=0.0, le=1.0,
        description=(
            "Fraction of factual claims in the answer that are directly "
            "supported by the cited context chunks. 1.0 = every claim "
            "traceable; 0.0 = entirely unsupported."
        ),
    )
    completeness: float = Field(
        ge=0.0, le=1.0,
        description=(
            "How fully the answer addresses the question. 1.0 = all "
            "parts answered; 0.5 = partial; 0.0 = doesn't answer."
        ),
    )
    critique: str = Field(
        description=(
            "If either score < 0.7: one sentence stating what is "
            "missing or unsupported, phrased as an instruction to the "
            "answering agent (e.g. 'Find and cite where X is "
            "configured'). Empty string if both ≥ 0.7."
        ),
    )


_RETRIEVAL_SYS = """You grade whether retrieved code/document chunks are
relevant to a question. Return ONLY the indices of relevant chunks."""

_ANSWER_SYS = """You are a strict grader for a code-RAG system. Score the
ANSWER against the QUESTION and CONTEXT on two axes:

- groundedness: are the claims supported by the context chunks?
- completeness: does it actually answer what was asked (intent: {intent})?

Be conservative — if a claim has no supporting chunk, it lowers
groundedness even if you believe it's true."""


_grader_llm = llm("router", temperature=0)
_chunk_grader = _grader_llm.with_structured_output(ChunkGrade)
_answer_grader = _grader_llm.with_structured_output(AnswerGrade)


def _format_chunks(chunks: list[dict], limit: int = MAX_GRADED_CHUNKS) -> str:
    parts = []
    for i, c in enumerate(chunks[:limit]):
        loc = c.get("file") or f"{c.get('repo','')}/{c.get('filepath','')}"
        text = (c.get("text") or c.get("code") or "")[:600]
        parts.append(f"[{i}] {loc}\n{text}")
    return "\n\n".join(parts)


def grade_retrieval(state: AgentState) -> tuple[dict, str | None]:
    """Returns (scores, refine_hint). Hint is None when retrieval is OK."""
    chunks = state.get("retrieved_chunks") or []
    query = state.get("query") or ""
    if not chunks or not query:
        return {"n_chunks": len(chunks), "n_relevant": 0}, None

    try:
        grade: ChunkGrade = _chunk_grader.invoke([
            SystemMessage(content=_RETRIEVAL_SYS),
            HumanMessage(content=(
                f"Question: {query}\n\nChunks:\n{_format_chunks(chunks)}"
            )),
        ])
        relevant = [i for i in grade.relevant if 0 <= i < len(chunks)]
    except Exception as e:
        log.warning("[evaluator] retrieval grading failed (%s); skipping", e)
        return {"n_chunks": len(chunks), "n_relevant": len(chunks),
                "skipped": True}, None

    scores = {
        "n_chunks": min(len(chunks), MAX_GRADED_CHUNKS),
        "n_relevant": len(relevant),
        "relevant_idx": relevant,
    }

    hint = None
    if len(relevant) < MIN_RELEVANT:
        irrelevant_files = [
            (chunks[i].get("file")
             or f"{chunks[i].get('repo','')}/{chunks[i].get('filepath','')}")
            for i in range(min(len(chunks), MAX_GRADED_CHUNKS))
            if i not in relevant
        ][:3]
        hint = (
            "Previous retrieval was weak — most chunks were irrelevant "
            f"(e.g. {', '.join(irrelevant_files)}). Try the `search` "
            "tool (it cascades code → docs → Confluence), or call "
            "`search_docs` WITHOUT a `repo` argument (Confluence pages "
            "are not repo-scoped), or use grep/find_symbol for exact "
            "identifiers."
        )

    return scores, hint


def grade_answer(state: AgentState) -> tuple[dict, str | None]:
    answer = state.get("answer") or ""
    query = state.get("query") or ""
    chunks = state.get("retrieved_chunks") or []
    intent = (state.get("query_plan") or {}).get("intent", "lookup")

    if not answer:
        return {"groundedness": 0.0, "completeness": 0.0}, "No answer produced."

    try:
        grade: AnswerGrade = _answer_grader.invoke([
            SystemMessage(content=_ANSWER_SYS.format(intent=intent)),
            HumanMessage(content=(
                f"Question: {query}\n\n"
                f"Context chunks:\n{_format_chunks(chunks)}\n\n"
                f"Answer:\n{answer}"
            )),
        ])
    except Exception as e:
        log.warning("[evaluator] answer grading failed (%s); skipping", e)
        return {"groundedness": 1.0, "completeness": 1.0,
                "skipped": True}, None

    scores = {
        "groundedness": round(grade.groundedness, 3),
        "completeness": round(grade.completeness, 3),
        "combined": round((grade.groundedness + grade.completeness) / 2, 3),
        "critique": grade.critique,
    }
    hint = grade.critique if scores["combined"] < ANSWER_THRESHOLD else None
    return scores, hint


def evaluator(state: AgentState) -> dict:
    """LangGraph node. Runs both graders; emits eval_scores + refine_hint."""
    # Skip simple (single-shot, latency-sensitive), blocked (guard
    # already wrote the answer), and research (subgraph has its own
    # synthesis; parent retrieved_chunks aren't its evidence).
    if state.get("route") in ("simple", "blocked", "research", "intro",
                               "skill"):
        return {}
    # Superdev: only grade read turns. Action turns mutate state, so
    # there's no "answer" to grade against retrieval — the deliverable
    # is the change itself.
    if (state.get("route") == "superdev"
            and state.get("turn_kind") == "action"):
        return {}

    eval_iter = (state.get("eval_iter") or 0) + 1
    prior = dict(state.get("eval_scores") or {})
    answer = state.get("answer") or ""
    chunks = state.get("retrieved_chunks") or []
    msgs = state.get("messages") or []

    has_cite = bool(re.search(r"\[SOURCE_\d+\]", answer))

    # Fast path: tool-grounded. If the agent called ≥1 tool this turn
    # AND the answer carries [SOURCE_N] citations, the LLM-judge adds
    # latency without changing the outcome (groundedness is what it's
    # checking, and we have direct evidence of it). output_guard still
    # verifies citations resolve and abstains on weak retrieval.
    n_tool_msgs = 0
    for m in reversed(msgs):
        if isinstance(m, HumanMessage):
            break
        if isinstance(m, ToolMessage):
            n_tool_msgs += 1
    if n_tool_msgs and has_cite:
        prior["answer"] = {"groundedness": 1.0, "completeness": 1.0,
                            "fast_path": "tool_grounded",
                            "n_tools": n_tool_msgs}
        log.debug("[evaluator] iter=%d fast-path=tool_grounded "
                  "(%d tools, cited) → pass", eval_iter, n_tool_msgs)
        return {"eval_scores": prior, "eval_iter": eval_iter,
                "refine_hint": None}

    # Fast path: agent explicitly said it found nothing. Skip the LLM
    # graders entirely and retry with a cascade/docs hint.
    if _EMPTY_ANSWER_RE.search(answer) and len(chunks) < MIN_RELEVANT:
        hint = (
            "Your answer says nothing was found, but the corpus has "
            "more sources. Call the `search` tool (it cascades code → "
            "local docs → Confluence automatically). If you already "
            "tried search_docs with a `repo` argument, retry WITHOUT "
            "it — Confluence/wiki pages are not repo-scoped."
        )
        # Superdev has no retry edge → don't promise a retry.
        will_retry = (eval_iter < MAX_EVAL_ITER
                      and state.get("route") != "superdev")
        prior["answer"] = {"groundedness": 0.0, "completeness": 0.0,
                            "combined": 0.0, "critique": hint,
                            "fast_path": "empty_answer"}
        log.debug("[evaluator] iter=%d fast-path=empty_answer → %s",
                  eval_iter, 'retry' if will_retry else 'pass')
        delta: dict = {
            "eval_scores": prior, "eval_iter": eval_iter,
            "refine_hint": hint if will_retry else None,
        }
        if will_retry:
            delta["messages"] = [HumanMessage(content=(
                f"[evaluator] {hint} Revise and answer with "
                f"[SOURCE_N] citations."))]
        return delta

    # Evidence-free turn (no chunks, no [SOURCE_N], not an explicit
    # "nothing found" answer). Conversational/meta replies like "why
    # is it scoped to X?" land here. Grading would judge them against
    # an empty context and either invent groundedness or hallucinate
    # a retry hint. Skip cheaply.
    if not chunks and not has_cite:
        log.debug("[evaluator] iter=%d skip: no chunks, no citations",
                  eval_iter)
        return {"eval_scores": prior, "eval_iter": eval_iter,
                "refine_hint": None}

    r_scores, r_hint = grade_retrieval(state)
    a_scores, a_hint = grade_answer(state)

    prior["retrieval"] = r_scores
    prior["answer"] = a_scores

    hint = a_hint or r_hint
    # Superdev has no retry edge — emitting refine_hint here would only
    # surface a misleading "retry #N" trace in the CLI without
    # actually re-running the loop. Force pass for that route.
    can_retry = state.get("route") != "superdev"
    log.debug(
        "[evaluator] iter=%d retrieval=%s/%s groundedness=%s "
        "completeness=%s → %s",
        eval_iter,
        r_scores.get('n_relevant'), r_scores.get('n_chunks'),
        a_scores.get('groundedness'), a_scores.get('completeness'),
        'retry' if hint and eval_iter < MAX_EVAL_ITER and can_retry
        else 'pass',
    )

    will_retry = bool(hint) and eval_iter < MAX_EVAL_ITER and can_retry
    delta: dict = {
        "eval_scores": prior,
        "eval_iter": eval_iter,
        "refine_hint": hint if will_retry else None,
    }
    if will_retry:
        delta["messages"] = [HumanMessage(
            content=(
                f"[evaluator] Your previous answer was insufficient. "
                f"{hint} Revise and answer again with [SOURCE_N] citations."
            )
        )]
    return delta


def should_retry(state: AgentState) -> str:
    """Conditional edge: 'retry' → agent_loop, 'pass' → output_guard."""
    return "retry" if state.get("refine_hint") else "pass"
