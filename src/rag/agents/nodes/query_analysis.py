"""
Query-analysis node: LLM-driven structured scope + intent extraction
that runs BEFORE clarify and BEFORE retrieval.

Replaces the lexical repo-disambiguation heuristic. Given the user's
question and the indexed repo inventory, a cheap model emits a
QueryPlan: which repos are in scope (a LIST — "all acme repos" → the 3
acme-* names, not "*"), the question intent, decomposed retrieval
queries, and whether a human is genuinely required. Clarify becomes a
fallback gated on plan.needs_human instead of a regex gatekeeper.
"""

import logging
from typing import Literal

from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel, Field

from ..llm import llm

log = logging.getLogger(__name__)
from ..state import AgentState
from ..tools._repo import available_repos
from ..verbosity import is_expand_request, parse_prefix
from .intro import is_intro_query


Intent = Literal[
    "lookup",
    "explain",
    "structural",
    "cross_repo_relationship",
    "compare",
    "debug",
    "other",
]


class QueryPlan(BaseModel):
    repo_scope: list[str] = Field(
        default_factory=list,
        description=(
            "Exact indexed repo names this question is about. Resolve "
            "shorthand and set descriptors against the provided inventory: "
            "'acme' or 'all acme repos' → every repo whose name contains "
            "'acme'. Empty list = no restriction (search everything)."
        ),
    )
    intent: Intent = Field(
        description="What kind of answer the user wants.",
    )
    search_queries: list[str] = Field(
        default_factory=list,
        description=(
            "1–4 focused retrieval queries the original question decomposes "
            "into. For relationship/compare intents, one per repo or per "
            "facet. For simple lookups, just the (possibly rewritten) "
            "original question."
        ),
    )
    needs_human: bool = Field(
        default=False,
        description=(
            "True if the question cannot be researched without more "
            "information. This includes: (a) a bare pronoun/referent with "
            "no history, OR (b) a repo reference that matches multiple "
            "indexed repos AND the surrounding context does not "
            "disambiguate which one. If context makes the choice obvious "
            "(e.g., the other tokens name a specific repo, or the user "
            "says 'all'), resolve it yourself and set False."
        ),
    )
    ambiguous_repos: list[str] = Field(
        default_factory=list,
        description=(
            "If needs_human is True because of repo ambiguity, the exact "
            "candidate repo names from the inventory that the ambiguous "
            "token could refer to. Empty otherwise."
        ),
    )
    clarify_question: str | None = Field(
        default=None,
        description="If needs_human is True, the single question to ask.",
    )


_SYSTEM = """You analyse a user's question for a code-RAG system and emit a
structured query plan. You do NOT answer the question.

Indexed repositories (the only valid repo_scope values):
{repos}

Rules:
- repo_scope MUST be a subset of the list above. Resolve descriptors:
  "all acme repos" → every listed repo containing "acme"; "the mgmt api"
  → the single best match. Plural/"all X" means a SET, not ambiguity.
- If a token matches multiple repos: resolve it yourself ONLY when
  the question makes the choice unambiguous (the user typed an exact
  repo name, an explicit prefix, or prior conversation pinned it).
  Otherwise — including when another token in the question is itself
  ambiguous — set needs_human=True, list ALL candidate repos for ALL
  ambiguous tokens in ambiguous_repos, and put a single which-repo
  question in clarify_question. Do NOT pick one based on naming-prefix
  similarity or guesswork.
- Otherwise only set needs_human when the question is unanswerable as
  written (no referent, no verb, etc.).
- search_queries should be what you would type into a code search box,
  not the literal user phrasing.{history_hint}"""


_analysis_llm = llm("router", temperature=0).with_structured_output(QueryPlan)


def query_analysis(state: AgentState) -> dict:
    query = (state.get("query") or "").strip()
    has_history = len(state.get("messages") or []) > 1

    # Verbosity meta-commands ("more", "expand", "elaborate") are
    # handled by the router as a re-answer of the PREVIOUS question.
    # Don't analyze or clarify them — preserve last turn's plan.
    if has_history and is_expand_request(query):
        prior = dict(state.get("query_plan") or {})
        prior["needs_human"] = False
        return {"query_plan": prior, "pending_clarification": None}

    # Strip "q:" / "detail:" / "tldr:" prefixes so the LLM analyzes the
    # actual question, not the verbosity directive.
    query, _ = parse_prefix(query)

    if not query:
        return {"query_plan": QueryPlan(intent="other").model_dump()}

    # Onboarding small-talk: skip the analysis LLM entirely (it would
    # likely flag needs_human or hallucinate scope). Router will see
    # the same regex and route="intro".
    if not has_history and is_intro_query(query):
        return {
            "query_plan": QueryPlan(intent="other",
                                    search_queries=[query]).model_dump(),
            "pending_clarification": None,
        }

    repos = available_repos(refresh=True)
    history_hint = (
        "\n- The user has prior conversation context; pronouns may refer to it."
        if has_history else ""
    )

    sys = _SYSTEM.format(
        repos="\n".join(f"  - {r}" for r in repos) or "  (none indexed)",
        history_hint=history_hint,
    )

    try:
        plan: QueryPlan = _analysis_llm.invoke([
            SystemMessage(content=sys),
            HumanMessage(content=query),
        ])
    except Exception as e:
        log.warning("[query_analysis] LLM failed (%s); deferring to "
                    "clarify heuristics", e)
        plan = QueryPlan(intent="other", search_queries=[query],
                         needs_human=True)

    # Validate repo_scope against the live inventory; drop hallucinated names.
    valid = set(repos)
    plan.repo_scope = [r for r in plan.repo_scope if r in valid]

    if not plan.search_queries:
        plan.search_queries = [query]

    # Once the user has answered ANY repo clarify, repo_scope is owned
    # by clarify (it merges per-token). Re-analysis must not add to it
    # — otherwise the LLM can inject the alternate candidate of a
    # not-yet-asked token, which makes cands ⊆ scope and suppresses
    # the next backstop round. The repo question is settled, so also
    # discard the LLM's ambiguous_repos.
    clarified = state.get("clarified") or {}
    prior_scope = (state.get("query_plan") or {}).get("repo_scope") or []
    if prior_scope and clarified.get("repo"):
        plan.repo_scope = [r for r in prior_scope if r in valid]
        plan.ambiguous_repos = []

    # If the LLM listed (near-)every repo as "ambiguous", it isn't
    # disambiguating — it's just saying "I don't know which one".
    # That's the referential case, not the repo case.
    if plan.ambiguous_repos and len(plan.ambiguous_repos) >= max(2, len(repos) - 1):
        plan.ambiguous_repos = []

    log.debug(
        "[query_analysis] intent=%s scope=%s sub_queries=%d "
        "needs_human=%s",
        plan.intent, plan.repo_scope or 'ALL',
        len(plan.search_queries), plan.needs_human,
    )

    # Referential follow-ups ("the function", "that file", "it") with
    # conversation history: the agent has the context to resolve
    # these; query_analysis doesn't. Don't interrupt — let the router
    # send it to agent_loop. Carry the PRIOR turn's plan forward
    # (scope, intent) so the agent stays scoped. Repo ambiguity is
    # the exception (the agent can't guess the user's repo intent).
    if plan.needs_human and has_history and not plan.ambiguous_repos:
        prior = state.get("query_plan") or {}
        plan.needs_human = False
        plan.clarify_question = None
        if not plan.repo_scope and prior.get("repo_scope"):
            plan.repo_scope = [r for r in prior["repo_scope"] if r in valid]
        if plan.intent == "other" and prior.get("intent"):
            plan.intent = prior["intent"]
        log.debug("[query_analysis] referential follow-up with history → "
                  "reusing prior plan (scope=%s)", plan.repo_scope or 'ALL')

    delta: dict = {"query_plan": plan.model_dump()}
    if plan.needs_human:
        amb = [r for r in plan.ambiguous_repos if r in valid]
        # Never re-ask a kind the user already answered this turn.
        if amb and "repo" not in clarified:
            from .clarify import ALL_MATCHES_OPTION
            delta["pending_clarification"] = {
                "kind": "repo",
                "matches": amb,
                "options": amb + [ALL_MATCHES_OPTION],
                "question": plan.clarify_question or (
                    f"That matches multiple repos: {', '.join(amb)}. "
                    f"Which one? (Reply '{ALL_MATCHES_OPTION}' or 'all' "
                    f"to use every match.)"
                ),
            }
        elif plan.clarify_question and "intent" not in clarified:
            delta["pending_clarification"] = {
                "kind": "intent",
                "options": [],
                "question": plan.clarify_question,
            }
    return delta
