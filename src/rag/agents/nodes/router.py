"""
Router node: cheap classifier that decides whether the query needs the
full agentic loop or can be answered with a single retrieve→generate pass.
"""

import logging
import re

from langchain_core.messages import SystemMessage, HumanMessage

from ..llm import llm

log = logging.getLogger(__name__)
from ..state import AgentState
from ..verbosity import parse_prefix, is_expand_request
from .intro import is_intro_query

_SYSTEM = """You are a query classifier for a code-RAG system.
Classify the user's question into exactly one of:

- simple: A conceptual lookup answerable with one semantic search.
  Examples: "what does function X do", "explain the auth flow",
  "what is the health endpoint", "show me the config for Z".

- agent: Needs structural code-graph queries, multi-step reasoning,
  reading full files, or web search — focused on ONE area.
  Examples: "what calls X", "where is X defined", "what breaks if I
  change X", "trace the flow from A to B", "will A run without B".

- research: Broad/comparison/audit across MULTIPLE repos or topics
  that benefits from parallel decomposition.
  Examples: "compare X across all repos", "audit all services for Y",
  "how do A, B, and C each handle Z", "summarize auth across the fleet".

If the question is a follow-up that references prior conversation
(uses "that", "it", "the one you mentioned", etc.) classify as: agent.

Respond with ONLY the single word: simple OR agent OR research."""

_router_llm = llm("router", temperature=0)

# Broad/comparison/audit questions → deep_research (parallel fan-out).
# Fallback when query_plan is absent; primary signal is plan.intent.
_RESEARCH_RE = re.compile(
    r"\bcompare\b"
    r"|\baudit\b"
    # 'research X across/in/about …' as a verb — NOT bare 'research'
    # (which false-positives on the noun, e.g., 'the research route',
    # 'deep research path').
    r"|\bresearch\s+(?!path\b|route\b|node\b|subgraph\b|mode\b)"
    r"(?:\w+\s+){1,3}(?:across|in|over|about|on)\b"
    r"|\bdeep\s+research\s+(?:on|into|about)\b"
    r"|\bsurvey\b"
    r"|\b(across|in|over)\s+(all|every|each)\b"
    r"|\b(all|every|each|both|these|those|multiple)\s+"
    r"(\w+\s+)?(repo|repos|service|services|codebases?)\b"
    r"|\bsummari[sz]e\b.*\b(across|all)\b"
    r"|\b\w+\s+(vs\.?|versus)\s+\w+\b",
    re.IGNORECASE,
)

_RESEARCH_INTENTS = frozenset({"compare", "cross_repo_relationship"})

# Doc/wiki-intent questions — use agent (it has search_docs);
# simple_rag only searches the code collection.
_DOCS_RE = re.compile(
    r"\b(runbooks?|design\s+docs?|wiki|confluence|documentation|"
    r"spec(?:ification)?s?|architecture\s+docs?|onboarding\s+(?:guide|doc)|"
    r"troubleshoot(?:ing)?\s+(?:guide|doc)|from\s+(?:the\s+)?docs?)\b",
    re.IGNORECASE,
)

# Git-history / JIRA-ticket questions — use agent (git_log/blame/show,
# find_commits_for_jira, jira_lookup). simple_rag has no commit data
# and would cascade all tiers for nothing.
_GIT_RE = re.compile(
    r"\b(commit(?:s|ted)?|git\s+(?:log|blame|show|history|diff)"
    r"|blame|change\s*log|recent\s+changes?|change\s+history"
    r"|(?:last|latest|recent)\s+\d+\s+commits?"
    r"|who\s+(?:changed|modified|wrote|authored|committed)"
    r"|when\s+was\b.*\b(?:changed|added|modified|introduced|removed)"
    r"|why\s+was\b.*\b(?:changed|added|removed|reverted)"
    r"|[A-Z][A-Z0-9]{1,9}-\d+)\b",
    re.IGNORECASE,
)

# Inventory / meta questions about the corpus itself — use agent (it
# has the repo_info tool); simple_rag would retrieve nothing.
_META_RE = re.compile(
    r"\b(what|which|list|show)\b.*\b(repos?|repositories|services?|"
    r"codebases?)\b.*\b(indexed|available|loaded|here|exist|"
    r"do (you|we) (have|know))\b"
    r"|\bwhat('?s| is)\s+indexed\b"
    r"|\b(list|show)\b.*\b(indexed|available)\s+(repos?|repositories|"
    r"services?)\b"
    r"|\b(list|show)\s+(me\s+)?(all\s+)?(the\s+)?repos?\b",
    re.IGNORECASE,
)

# Structural questions that should always hit the code graph via agent_loop.
_STRUCTURAL_RE = re.compile(
    r"\b(what|who)\s+(calls?|invokes?|uses?)\b"
    r"|\bwhere\s+is\b.*\b(defined|declared|implemented)\b"
    r"|\bwhat\s+does\b.*\bcall\b"
    r"|\b(callers?|callees?)\s+of\b"
    r"|\bcall\s+(graph|chain|hierarchy)\b"
    r"|\bwhat\s+breaks\b|\bimpact\s+of\b"
    r"|\b(connects?|talks?)\s+to\b"
    r"|\bdepends?\s+on\b|\bdependenc(y|ies)\b"
    r"|\b(list|what|which)\b.*\bservices?\b"
    r"|\b(exposes?|endpoints?)\b",
    re.IGNORECASE,
)


def router(state: AgentState) -> dict:
    query = state["query"]
    history = state.get("messages", [])
    has_history = len(history) > 1
    plan = state.get("query_plan") or {}
    scope = plan.get("repo_scope") or []

    delta: dict = {}

    # Option D: bare "more"/"expand" → re-answer the previous question
    # at detailed verbosity. agent_loop sees the full message history so
    # it knows what "more" refers to.
    if has_history and is_expand_request(query):
        log.debug("[router] expand request → detailed re-answer")
        return {"route": "agent", "verbosity": "detailed"}

    # Option B: inline prefix "q: …" / "detail: …" overrides verbosity.
    stripped, pfx_level = parse_prefix(query)
    if pfx_level:
        delta["verbosity"] = pfx_level
        delta["query"] = stripped
        query = stripped

    log.debug("[router] history_len=%d verbosity=%s query=%r",
              len(history),
              delta.get('verbosity') or state.get('verbosity') or 'normal',
              query)

    # Onboarding small-talk ("what can I do here", "get me started")
    # → static intro with example queries. Only on a fresh session —
    # mid-conversation it's likely referential.
    if not has_history and is_intro_query(query):
        return {**delta, "route": "intro"}

    # Skills: explicit invocation by name ("/<name> …" or "run
    # skill <name> …") takes precedence over trigger regexes —
    # exact get(), no fuzzy match.
    from ...skills import match as match_skill, get as get_skill
    m = re.match(
        r"^\s*(?:/|run\s+skill\s+|run\s+the\s+)([a-z][a-z0-9-]{1,40})\b",
        query, re.IGNORECASE)
    if m and (sk := get_skill(m.group(1).lower())):
        log.debug("[router] → skill:%s (explicit)", sk.name)
        return {**delta, "route": "skill", "skill_name": sk.name}
    # Then per-skill trigger regexes (data/skills/*.md frontmatter).
    if (sk := match_skill(query)):
        log.debug("[router] → skill:%s (trigger)", sk.name)
        return {**delta, "route": "skill", "skill_name": sk.name}

    # Inventory/meta questions ("what repos are indexed") → agent
    # (repo_info tool). Check BEFORE research/has_history so "list all
    # repos" isn't mistaken for a fan-out survey.
    if _META_RE.search(query):
        return {**delta, "route": "agent"}

    # Doc-intent questions ("runbook", "design doc", "from docs",
    # "confluence") → agent (search_docs tool). simple_rag only
    # queries the code collection and would abstain.
    if _DOCS_RE.search(query):
        return {**delta, "route": "agent"}

    # Git-history / JIRA → agent (git_* + jira_* tools). simple_rag
    # has no commit/ticket data and would cascade all tiers uselessly.
    if _GIT_RE.search(query):
        return {**delta, "route": "agent"}

    # Broad multi-target → research (parallel fan-out). Check this FIRST,
    # before the has_history short-circuit: comparison/audit questions are
    # self-contained (planner gets the full query) and benefit from fan-out
    # regardless of whether this is a follow-up turn.
    #
    # Triggers are EXPLICIT only: the analysis LLM classified intent as
    # compare/cross-repo, or the user's phrasing says so. Repo-scope size
    # and sub-query count are NOT triggers — a symbol lookup that happens
    # to span 3 repos is still a lookup, not a survey.
    if plan.get("intent") in _RESEARCH_INTENTS or _RESEARCH_RE.search(query):
        why = (plan.get("intent") if plan.get("intent") in _RESEARCH_INTENTS
               else "regex")
        log.debug("[router] → research (%s; scope=%d)", why, len(scope))
        return {**delta, "route": "research"}

    # If there is prior conversation, ALWAYS route to agent. simple_rag is
    # stateless (does fresh retrieval on the literal query); only agent_loop
    # reads history. Agent will answer from context cheaply (often 0 tool
    # calls) if the answer is already in the conversation.
    if has_history:
        return {**delta, "route": "agent"}

    # Structural questions → agent (graph tools are more precise than search).
    if plan.get("intent") == "structural" or _STRUCTURAL_RE.search(query):
        return {**delta, "route": "agent"}

    # Fresh conversation — classify by query complexity.
    response = _router_llm.invoke([
        SystemMessage(content=_SYSTEM),
        HumanMessage(content=query),
    ])
    decision = response.content.strip().lower()
    if decision not in ("simple", "agent", "research"):
        decision = "agent"
    return {**delta, "route": decision}


def route_decision(state: AgentState) -> str:
    """Conditional-edge function: returns the next node name."""
    return state["route"]
