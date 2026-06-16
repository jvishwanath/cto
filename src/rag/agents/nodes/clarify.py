"""
Clarify node: human-in-the-loop pause point.

When the agent cannot proceed without user input — ambiguous repo
reference, an upstream tool flagged ambiguity, or an under-specified
query — this node surfaces a question via langgraph.types.interrupt(),
checkpoints, and resumes once the caller replies with
Command(resume=<answer>).
"""

import logging
import re

from langchain_core.messages import HumanMessage
from langgraph.types import interrupt

from ..state import AgentState

log = logging.getLogger(__name__)
from ..tools._repo import available_repos, resolve_repo, _segments
from ..verbosity import is_expand_request
from .router import _DOCS_RE

MAX_CLARIFY_ROUNDS = 3

_REPO_TOKEN_RE = re.compile(r"\b[A-Za-z][\w-]{2,}\b")

_SKIP = frozenset({
    "the", "and", "for", "with", "from", "into", "what", "where", "who",
    "how", "why", "does", "call", "calls", "uses", "used", "show", "list",
    "find", "file", "files", "repo", "repos", "code", "service", "services",
    "function", "method", "class", "this", "that", "which",
})


ALL_MATCHES_OPTION = "(all of these)"


def _round_count(clarified: dict | None) -> int:
    """Actual clarify rounds, ignoring marker keys (repo_token:*)."""
    return sum(1 for k in (clarified or {}) if not k.startswith("repo_token:"))
_ALL_ANSWER_RE = re.compile(r"\b(all|every|any|each|everywhere|no filter|don'?t filter)\b",
                            re.IGNORECASE)


def _all_ambiguous_repos(query: str,
                         skip_tokens: set[str] | None = None) -> list[dict]:
    """Every ambiguous repo token in the query, in order of appearance.
    Each dict has the same shape as _ambiguous_repo's return."""
    repos = available_repos()
    if len(repos) < 2:
        return []
    skip = {t.lower() for t in (skip_tokens or set())}
    seen: set[str] = set()
    out: list[dict] = []
    for tok in _REPO_TOKEN_RE.findall(query):
        low = tok.lower()
        if low in _SKIP or low in seen or low in skip:
            continue
        seen.add(low)
        resolved, err = resolve_repo(tok)
        if resolved is not None or err is None or "ambiguous" not in err:
            continue
        # Segment-aware (same logic as resolve_repo) so 'llm' doesn't
        # match 'enro·llm·ent'.
        tok_segs = [s for s in _segments(tok) if len(s) >= 3]
        matches = [r for r in repos
                   if any(qs == rs or rs.startswith(qs) or rs.endswith(qs)
                          or qs.startswith(rs)
                          for qs in tok_segs for rs in _segments(r))]
        if len(matches) >= len(repos):
            continue
        out.append({
            "kind": "repo",
            "token": tok,
            "matches": matches,
            "options": matches + [ALL_MATCHES_OPTION],
            "question": (
                f"'{tok}' matches multiple repos: {', '.join(matches)}. "
                f"Which one did you mean? (Reply '{ALL_MATCHES_OPTION}' or "
                f"'all' to use every match.)"
            ),
        })
    return out


def _ambiguous_repo(query: str,
                    skip_tokens: set[str] | None = None) -> dict | None:
    repos = available_repos()
    if len(repos) < 2:
        return None

    skip = {t.lower() for t in (skip_tokens or set())}
    seen: set[str] = set()
    for tok in _REPO_TOKEN_RE.findall(query):
        low = tok.lower()
        if low in _SKIP or low in seen or low in skip:
            continue
        seen.add(low)

        resolved, err = resolve_repo(tok)
        if resolved is not None or err is None:
            continue
        if "ambiguous" not in err:
            continue

        # Segment-aware (same logic as resolve_repo) so 'llm' doesn't
        # match 'enro·llm·ent'.
        tok_segs = [s for s in _segments(tok) if len(s) >= 3]
        matches = [r for r in repos
                   if any(qs == rs or rs.startswith(qs) or rs.endswith(qs)
                          or qs.startswith(rs)
                          for qs in tok_segs for rs in _segments(r))]

        # If the token matches ALL repos (common prefix like 'acme'),
        # it's not a disambiguator — skip. Only ask when the token
        # actually narrows the candidate set.
        if len(matches) >= len(repos):
            continue

        return {
            "kind": "repo",
            "token": tok,
            "matches": matches,
            "options": matches + [ALL_MATCHES_OPTION],
            "question": (
                f"'{tok}' matches multiple repos: {', '.join(matches)}. "
                f"Which one did you mean? (Reply '{ALL_MATCHES_OPTION}' or "
                f"'all' to use every match.)"
            ),
        }
    return None


def needs_clarification(state: AgentState) -> dict | None:
    """
    Returns {"kind", "question", "options", ...} or None.
    Priority: explicit pending_clarification → repo ambiguity → tiny query.
    Each kind is asked at most once per turn (tracked via clarified).
    """
    query = (state.get("query") or "").strip()
    has_history = len(state.get("messages") or []) > 1

    # "more"/"expand" is a router meta-command, never a clarify target.
    if has_history and is_expand_request(query):
        return None

    pending = state.get("pending_clarification")
    if pending:
        return pending

    if not query:
        return None

    already = set((state.get("clarified") or {}).keys())
    plan = state.get("query_plan") or {}
    scope = set(plan.get("repo_scope") or [])

    # Doc-intent queries ("runbook", "from docs", "confluence") don't
    # need repo disambiguation — search_docs doesn't filter by repo,
    # and Confluence pages aren't repo-keyed. Skip straight past the
    # repo backstop; the agent will scope via relates_to if relevant.
    docs_intent = bool(_DOCS_RE.search(query))

    # Lexical repo disambiguation: backstop for when query_analysis
    # silently committed to ONE candidate of an ambiguous token.
    asked_tokens = {
        v for k, v in (state.get("clarified") or {}).items()
        if k.startswith("repo_token:")
    }
    # The backstop's job: catch when query_analysis silently picked
    # ONE of an ambiguous token's N candidates. So only fire when the
    # token's candidate set OVERLAPS the LLM's scope (it picked from
    # this ambiguity) but isn't fully contained (it left some out).
    # If candidates ∩ scope = ∅, the LLM didn't treat this token as a
    # repo reference at all — trust that. (e.g., 'LLM' in a question
    # about a specific repo isn't a repo shorthand, even if it
    # accidentally fuzzy-matched something.)
    plan_resolved = bool(scope) and plan.get("needs_human") is False
    all_amb = [] if docs_intent else [
        a for a in _all_ambiguous_repos(query, skip_tokens=asked_tokens)
        if not (set(a["matches"]) <= scope
                or any(r.lower() in query.lower() for r in a["matches"])
                or (plan_resolved and not (set(a["matches"]) & scope)))
    ]
    rounds_left = MAX_CLARIFY_ROUNDS - _round_count(state.get("clarified"))
    if len(all_amb) > rounds_left and "repo_bulk" not in already:
        # Too many to ask one-by-one — collapse into a single
        # free-form request listing every ambiguity.
        lines = [f"  • '{a['token']}' → {', '.join(a['matches'])}"
                 for a in all_amb]
        all_cands = sorted({r for a in all_amb for r in a["matches"]})
        return {
            "kind": "repo_bulk",
            "tokens": [a["token"] for a in all_amb],
            "matches": all_cands,
            "options": [],
            "question": (
                f"{len(all_amb)} repo references are ambiguous:\n"
                + "\n".join(lines)
                + "\n\nPlease reply with the exact repo names you want "
                  "(comma- or space-separated), or 'all' to use every "
                  "match listed."
            ),
        }
    if all_amb:
        return all_amb[0]

    # If query_analysis ran and decided no human is needed, trust it
    # over the tiny-query heuristic.
    if plan.get("needs_human") is False:
        return None

    if "intent" not in already and not has_history and len(query.split()) < 3:
        return {
            "kind": "intent",
            "options": [],
            "question": (
                "Could you be more specific? For example: which repo, "
                "and are you after a definition, callers, or an explanation?"
            ),
        }

    return None


def clarify(state: AgentState) -> dict:
    """LangGraph node. interrupt() pauses the graph; on resume,
    fold the user's answer back into state."""
    req = needs_clarification(state)
    if not req:
        return {}

    # Headless (Phase 9-C): no human to answer. Auto-resolve by
    # picking ALL candidates (the conservative degenerate) and
    # log it; never interrupt().
    if state.get("headless"):
        kind = req["kind"]
        if kind in ("repo", "repo_bulk"):
            answer = "all"
        elif kind == "intent":
            answer = "(headless — proceed with broadest scope)"
        else:
            answer = "skip"
        log.debug("[clarify] headless kind=%s → auto=%r", kind, answer)
        return _apply_answer(state, req, answer)

    log.debug("[clarify] kind=%s → interrupting", req['kind'])
    answer = interrupt(req)
    answer = str(answer).strip()
    log.debug("[clarify] resumed with answer=%r", answer)
    return _apply_answer(state, req, answer)


def _apply_answer(state: AgentState, req: dict, answer: str) -> dict:
    answer = str(answer).strip()
    kind = req["kind"]
    original_q = state.get("query", "")
    plan = dict(state.get("query_plan") or {})

    if kind == "repo_bulk":
        repos = set(available_repos())
        candidates: list[str] = req.get("matches") or []
        if _ALL_ANSWER_RE.search(answer):
            chosen = list(candidates)
        else:
            # Free-form: extract every word/token that is an exact repo
            # name (or resolves uniquely). Order by first appearance.
            chosen, seen = [], set()
            for tok in re.split(r"[,\s]+", answer):
                tok = tok.strip()
                if not tok:
                    continue
                r = tok if tok in repos else resolve_repo(tok)[0]
                if r and r not in seen:
                    seen.add(r); chosen.append(r)
        if not chosen:
            chosen = list(candidates)
            clarification_text = (
                f"(No recognized repo names in {answer!r} — using all "
                f"candidates: {', '.join(chosen)})"
            )
        else:
            clarification_text = f"(Use repos: {', '.join(chosen)})"
        # Replace each ambiguous token in the query with the chosen
        # repo(s) that intersect its candidate set, so re-analysis sees
        # exact names.
        new_query = original_q
        for a in _all_ambiguous_repos(original_q):
            picks = [r for r in chosen if r in a["matches"]] or a["matches"]
            new_query = re.sub(re.escape(a["token"]),
                               "/".join(picks), new_query,
                               count=1, flags=re.IGNORECASE)
        prior_scope = set(plan.get("repo_scope") or [])
        plan["repo_scope"] = sorted(
            (prior_scope - set(candidates)) | set(chosen)
        )
        clarified = {"repo": plan["repo_scope"], "repo_bulk": True}
        for t in (req.get("tokens") or []):
            clarified[f"repo_token:{t.lower()}"] = t

    elif kind == "repo":
        token = req.get("token") or ""
        matches: list[str] = req.get("matches") or req.get("options", [])[:-1]
        resolved, _ = resolve_repo(answer)
        if resolved:
            chosen = [resolved]
            new_query = (re.sub(re.escape(token), resolved, original_q,
                                flags=re.IGNORECASE)
                         if token else original_q)
            clarification_text = f"(Use repo: {resolved})"
        else:
            # "all" / "(all of these)" / unrecognized → scope to the
            # MATCHED candidate set, not the whole index.
            chosen = matches
            new_query = original_q
            if answer == ALL_MATCHES_OPTION or _ALL_ANSWER_RE.search(answer):
                clarification_text = f"(Use repos: {', '.join(chosen)})"
            else:
                clarification_text = (
                    f"(Repo '{answer}' not recognized — using: "
                    f"{', '.join(chosen)})"
                )
        # MERGE into repo_scope: drop this token's other candidates
        # AND any candidate of still-unresolved ambiguous tokens (so
        # the LLM's guess for those can't pre-satisfy the backstop).
        # Repos confirmed by earlier clarify rounds are preserved.
        prior_scope = set(plan.get("repo_scope") or [])
        prior_clarified = state.get("clarified") or {}
        confirmed = set()
        for k, v in prior_clarified.items():
            if k == "repo" or k.startswith("repo_"):
                if isinstance(v, list):
                    confirmed.update(v)
        asked = {v for k, v in prior_clarified.items()
                 if k.startswith("repo_token:")} | {token.lower()}
        pending_cands = {
            r for a in _all_ambiguous_repos(new_query, skip_tokens=asked)
            for r in a["matches"]
        }
        plan["repo_scope"] = sorted(
            ((prior_scope - set(matches) - pending_cands) | confirmed
             | set(chosen))
        )
        clarified = {"repo": plan["repo_scope"]}
        if token:
            clarified[f"repo_token:{token.lower()}"] = token
        else:
            # query_analysis-driven repo clarify carries no `token`, so
            # mark every query token whose ambiguity set equals the
            # candidates we just resolved. Without this, the lexical
            # backstop re-detects e.g. 'mgmtapi' and asks again. The
            # token text stays in the query; the marker tells the
            # backstop it's already answered (skip_tokens).
            match_set = set(matches)
            for a in _all_ambiguous_repos(original_q):
                if set(a["matches"]) & match_set:
                    clarified[f"repo_token:{a['token'].lower()}"] = a["token"]
    elif kind == "intent":
        # COMBINE original short query with the user's elaboration —
        # don't replace, or the original intent ("auth flow") is lost.
        new_query = f"{original_q} — {answer}"
        clarified = {"intent": answer}
        clarification_text = answer
    else:
        new_query = original_q
        clarified = {kind: answer}
        clarification_text = f"({kind}: {answer})"

    # CRITICAL: agent_loop reads state["messages"], not state["query"].
    # Append the clarification as a HumanMessage so the agent actually
    # sees it. Without this, the rewritten query is invisible to the LLM.
    #
    # Accumulate clarified across rounds so the round-counter in
    # should_clarify works and so all answers are visible downstream.
    prior = dict(state.get("clarified") or {})
    for k, v in clarified.items():
        key = k if k not in prior else f"{k}_{len(prior)+1}"
        prior[key] = v

    delta: dict = {
        "query": new_query,
        "messages": [HumanMessage(
            content=f"[clarification] {clarification_text}"
        )],
        "pending_clarification": None,
        "clarified": prior,
    }
    if plan:
        delta["query_plan"] = plan
    return delta


def should_clarify(state: AgentState) -> str:
    """Conditional-edge function: 'clarify' | 'continue'.

    Primary signal is query_plan.needs_human (set by query_analysis).
    The lexical heuristics in needs_clarification() are a fallback for
    when the plan is absent. Capped at MAX_CLARIFY_ROUNDS."""
    if _round_count(state.get("clarified")) >= MAX_CLARIFY_ROUNDS:
        return "continue"

    return "clarify" if needs_clarification(state) else "continue"
