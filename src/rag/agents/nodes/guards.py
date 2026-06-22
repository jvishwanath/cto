"""
Guardrails (Phase 4.5).

Two pure-Python nodes bracketing the answer pipeline:

  input_guard  — first node after reset_turn. Redacts PII in the
                 user's question, blocks obvious jailbreak/injection
                 patterns BEFORE any LLM call or clarify interrupt.
                 Defense-in-depth: the corp LiteLLM gateway already
                 runs its own guardrail layer, but that's opaque
                 and doesn't help when the gateway is bypassed in
                 tests.

  output_guard — runs after the evaluator passes. Scrubs secrets from
                 the answer, verifies every [SOURCE_N] resolves to a
                 real retrieved chunk or footnote, and abstains when
                 retrieval confidence is uniformly low.

Both write to state.guard_flags so the UI/API can surface what was
applied.
"""

import logging
import re

from pydantic import BaseModel, Field
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, SystemMessage

from ..state import AgentState
from ..llm import llm
from ...config import OFF_TOPIC_CHECK_ENABLED

log = logging.getLogger(__name__)

# ── PII / secret patterns ────────────────────────────────────────────
# Ordered: longer / more specific first so a JWT isn't partially
# masked by the base64-ish AWS pattern, etc.
_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws_secret", re.compile(
        r"(?i)aws_?secret\w*\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?")),
    ("private_key", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
        r"[\s\S]+?-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("bearer_token", re.compile(
        r"(?i)\b(?:bearer|authorization:\s*bearer)\s+([A-Za-z0-9._~+/=-]{20,})")),
    ("api_key", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*"
        r"['\"]?([A-Za-z0-9._~+/=-]{16,})['\"]?")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]

# ── Jailbreak / prompt-injection heuristics ──────────────────────────
_JAILBREAK_RE = re.compile(
    r"ignore (?:all|the|any)?\s*(?:above|previous|prior)\s+instructions"
    r"|disregard (?:your|all|the)\s+(?:system\s+)?(?:prompt|instructions|rules)"
    r"|\bDAN\b.*\bmode\b"
    r"|you (?:are|act as) (?:now\s+)?(?:no longer|not) (?:bound|restricted|an? AI)"
    r"|reveal (?:your|the)\s+system\s+prompt"
    r"|\bBEGIN\s+SYSTEM\s+PROMPT\b"
    r"|<\s*/?\s*system\s*>",
    re.IGNORECASE,
)

# Off-topic: questions clearly not about the indexed corpus.
_OFFTOPIC_RE = re.compile(
    r"\b(weather|stock price|recipe|lyrics|poem|joke about|horoscope|"
    r"who (?:won|is the president))\b",
    re.IGNORECASE,
)

_INLINE_CITE_RE = re.compile(r"\[SOURCE_(\d+)\]")
_FOOTNOTE_RE = re.compile(r"^\[SOURCE_(\d+)\]:\s*`?([^`\n]+?)`?\s*$", re.MULTILINE)

# Retrieval-relatedness bands (max rerank score across chunks):
#   >= ABSTAIN_THRESHOLD          → grounded; answer from chunks
#   [ADJACENT_THRESHOLD, ABSTAIN) → domain-adjacent; allow a general
#                                    answer, note it's not corpus-grounded
#   <  ADJACENT_THRESHOLD         → off-domain; general answer kept but
#                                    flagged as unrelated to the corpus
# Band thresholds are heuristic (cross-encoder scores aren't calibrated)
# — tune against real queries. See [[topic-classification-followup]] for
# the planned LLM-based replacement (approach C).
ABSTAIN_THRESHOLD = 0.30
ADJACENT_THRESHOLD = 0.12
ABSTAIN_TEXT = (
    "I couldn't find enough relevant context in the indexed repositories "
    "to answer that confidently. Try rephrasing, naming a specific repo or "
    "file, or asking a narrower question."
)
_ADJACENT_NOTE = (
    "\n\n_ℹ︎ Answered from general knowledge — this is related to the "
    "indexed code/docs but no source was strong enough to cite. Ask a "
    "more specific question to pull in the actual implementation._"
)
_OFFDOMAIN_NOTE = (
    "\n\n_ℹ︎ Answered from general knowledge — this appears unrelated to "
    "the indexed repositories/docs, so it isn't grounded in your corpus. "
    "Verify against an authoritative source._"
)


def redact(text: str) -> tuple[str, list[str]]:
    """Mask PII/secret patterns in-place. Returns (redacted_text, kinds_hit)."""
    hits: list[str] = []
    out = text
    for kind, pat in _PII_PATTERNS:
        new, n = pat.subn(f"[REDACTED:{kind}]", out)
        if n:
            hits.extend([kind] * n)
            out = new
    return out, hits


def detect_jailbreak(text: str) -> str | None:
    m = _JAILBREAK_RE.search(text)
    return m.group(0) if m else None


# ── Nodes ────────────────────────────────────────────────────────────

class DomainGrade(BaseModel):
    is_on_domain: bool = Field(
        description=(
            "True if the query is strictly about software engineering, "
            "programming, repositories, codebases, APIs, database schemas, "
            "computer science algorithms, data structures, or technical documentation. "
            "False if it is about sports, general news, politics, general history, "
            "hobbies, recipes, or casual chit-chat."
        )
    )

_domain_classifier = llm("router", temperature=0).with_structured_output(DomainGrade)


def input_guard(state: AgentState) -> dict:
    query = (state.get("query") or "").strip()
    if not query:
        return {"guard_flags": {}}

    history = state.get("messages", [])
    has_history = len(history) > 1

    flags: dict = {}

    redacted, kinds = redact(query)
    if kinds:
        flags["pii_redacted"] = sorted(set(kinds))
        log.debug("[input_guard] redacted %d item(s): %s",
                  len(kinds), flags['pii_redacted'])

    blocked: str | None = None
    if (jb := detect_jailbreak(redacted)):
        blocked = f"prompt-injection pattern: {jb!r}"
    elif OFF_TOPIC_CHECK_ENABLED and _OFFTOPIC_RE.search(redacted):
        blocked = "off-topic for this code-RAG system"
    elif OFF_TOPIC_CHECK_ENABLED and not has_history:
        # LLM-as-a-Judge semantic domain check
        try:
            grade: DomainGrade = _domain_classifier.invoke([
                SystemMessage(content=(
                    "You are a strict input guardrail for a code-RAG system. "
                    "Determine if the user's query is about software engineering, "
                    "programming, codebase architecture, computer science algorithms, "
                    "or databases. Return is_on_domain=False for general knowledge, "
                    "geopolitics, general news, sports, history, recipes, or casual chat."
                )),
                HumanMessage(content=redacted)
            ])
            if not grade.is_on_domain:
                blocked = "generic or out-of-domain query"
        except Exception as e:
            log.warning("[input_guard] LLM classification failed (%s); falling back to regex", e)

    delta: dict = {}
    if blocked:
        flags["blocked"] = blocked
        msg = (f"⛔ Request blocked by input guard ({blocked}). "
               f"This system answers questions about the indexed code "
               f"repositories only.")
        log.info("[input_guard] BLOCKED — %s", blocked)
        delta = {
            "answer": msg,
            "messages": [AIMessage(content=msg)],
            "route": "blocked",
            "citations": [],
        }
    else:
        # Always propagate the redacted query so downstream LLM nodes
        # never see the raw PII, even when redaction was a no-op.
        delta = {"query": redacted}
        if kinds:
            delta["messages"] = [HumanMessage(
                content=f"[input_guard] PII redacted: {', '.join(sorted(set(kinds)))}"
            )]

    delta["guard_flags"] = flags
    return delta


def input_guard_route(state: AgentState) -> str:
    """Conditional edge: 'blocked' short-circuits straight to respond."""
    return "blocked" if (state.get("guard_flags") or {}).get("blocked") else "continue"


def output_guard(state: AgentState) -> dict:
    answer = state.get("answer") or ""
    chunks = state.get("retrieved_chunks") or []
    flags: dict = dict(state.get("guard_flags") or {})
    delta: dict = {}

    # 1. Abstain when retrieval was weak — BUT only when the answer
    #    actually depends on that retrieval. If the agent answered
    #    without citing any [SOURCE_N] (e.g. from parametric/general
    #    knowledge), the weak chunks are irrelevant: don't nuke a good
    #    answer, just tag its provenance. Abstain only when the answer
    #    leans on chunks that are all below threshold.
    #    Skip for blocked (guard already answered) and research
    #    (subgraph chunks aren't surfaced here).
    scores = [
        c["rerank_score"] if "rerank_score" in c else c.get("score", 0.0)
        for c in chunks
    ]
    max_score = max(scores) if scores else 0.0
    skip_band = state.get("route") in ("blocked", "research", "intro")

    # On the agent route, `retrieved_chunks` only captures search/
    # search_code/search_docs — NOT read_file, grep, find_symbol, or
    # git/jira tools. An agent that grounded its answer via those reads
    # would be wrongly abstained if we judged it on the (incomplete,
    # low-scoring) search chunks alone. So when the agent actually used
    # tools this turn, trust its grounding and skip the score-based
    # abstain. (output_guard still scrubs secrets + verifies citations.)
    if state.get("route") == "agent":
        msgs = state.get("messages") or []
        used_tools = False
        for m in reversed(msgs):
            if isinstance(m, HumanMessage):
                break
            if isinstance(m, ToolMessage):
                used_tools = True
                break
        if used_tools:
            skip_band = True

    weak_retrieval = bool(chunks) and max_score < ABSTAIN_THRESHOLD and not skip_band
    cites_chunks = bool(_INLINE_CITE_RE.search(answer))

    if OFF_TOPIC_CHECK_ENABLED and weak_retrieval and (cites_chunks or not answer):
        # Answer is grounded in weak chunks (or there's no answer) →
        # confabulation risk → abstain.
        flags["abstained"] = True
        flags["max_retrieval_score"] = round(max_score, 3)
        log.info("[output_guard] abstaining — max score %.3f < %s, "
                 "answer cites chunks", max_score, ABSTAIN_THRESHOLD)
        return {
            "answer": ABSTAIN_TEXT,
            "messages": [AIMessage(content=ABSTAIN_TEXT)],
            "citations": [],
            "guard_flags": flags,
        }

    if OFF_TOPIC_CHECK_ENABLED and weak_retrieval and answer and not cites_chunks:
        # Uncited general-knowledge answer over weak retrieval. Two
        # bands by relatedness (approach A): domain-adjacent keeps a
        # softer note; off-domain flags it as unrelated. Either way
        # the answer survives — we don't nuke a good general answer.
        adjacent = max_score >= ADJACENT_THRESHOLD
        flags["ungrounded"] = True
        flags["relatedness"] = "adjacent" if adjacent else "off_domain"
        flags["max_retrieval_score"] = round(max_score, 3)
        answer = answer + (_ADJACENT_NOTE if adjacent else _OFFDOMAIN_NOTE)
        delta["answer"] = answer
        delta["messages"] = [AIMessage(content=answer)]
        log.info("[output_guard] uncited answer, max score %.3f → %s, "
                 "kept with note", max_score,
                 'adjacent' if adjacent else 'off-domain')

    if not answer:
        return {"guard_flags": flags}

    # 2. Scrub secrets that leaked from indexed source into the answer.
    scrubbed, kinds = redact(answer)
    if kinds:
        flags["secrets_scrubbed"] = sorted(set(kinds))
        log.info("[output_guard] scrubbed %d secret(s): %s",
                 len(kinds), flags['secrets_scrubbed'])
        answer = scrubbed
        delta["answer"] = answer
        delta["messages"] = [AIMessage(content=answer)]

    # 3. Citation-verify: every inline [SOURCE_N] must resolve to a
    #    footnote line or (simple route) a retrieved-chunk index.
    footnotes = {int(n) for n, _ in _FOOTNOTE_RE.findall(answer)}
    cited = {int(n) for n in _INLINE_CITE_RE.findall(answer)}
    valid_idx = set(range(len(chunks)))
    orphans = sorted(
        n for n in cited
        if n not in footnotes
        and not (state.get("route") == "simple" and n in valid_idx)
    )
    if orphans:
        flags["orphan_citations"] = orphans
        note = (f"\n\n_⚠ Unverified citations: "
                f"{', '.join(f'[SOURCE_{n}]' for n in orphans)} — "
                f"no matching source found._")
        answer += note
        delta["answer"] = answer
        delta["messages"] = [AIMessage(content=answer)]
        log.info("[output_guard] orphan citations: %s", orphans)

    delta["guard_flags"] = flags
    return delta
