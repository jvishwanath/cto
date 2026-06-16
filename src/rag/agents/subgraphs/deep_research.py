"""
Deep-research subgraph: planner → parallel fan-out (Send) → synthesize.

For broad/comparison/audit questions ("compare X across all repos",
"audit all repos for Y"), decompose into N sub-questions and research
them concurrently using LangGraph's Send API, then synthesize a single
answer with citations.
"""

import json
import operator
import re
from typing import TypedDict, Annotated

from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

from ..llm import llm
from ..tools._repo import available_repos
from ..verbosity import instruction as verbosity_instruction, resolve as verbosity_resolve
from ...retrieval import hybrid_search, rerank
from ...ingest.docs import DOCS_COLLECTION

MAX_SUBQUESTIONS = 6
PER_BRANCH_TOP_K = 5


class ResearchState(TypedDict, total=False):
    query: str
    verbosity: str
    query_plan: dict
    sub_questions: list[dict]
    findings: Annotated[list, operator.add]
    answer: str
    citations: list


_PLANNER_SYSTEM = """You decompose a broad question about a codebase into
2–{maxn} focused sub-questions for parallel research.

Indexed repositories: {repos}

For each sub-question, pick:
- "repo": exact repo name from the list above, or null to search all
- "kind": "code" (search source) or "docs" (search PDFs/design docs)

Return ONLY a JSON array:
[{{"q": "...", "repo": "acme-auth"|null, "kind": "code"|"docs"}}, ...]

Prefer one sub-question per repo for comparison queries. Keep each
sub-question self-contained."""

_SYNTH_SYSTEM = """You synthesize findings from parallel research branches
into one coherent answer.

Cite sources inline as [SOURCE_N]. At the end, list each cited source on
its own line: [SOURCE_N]: repo/path/to/file.ext

If the question is a comparison, structure the answer as a comparison
(table or per-repo sections).

ALWAYS refer to repositories by their exact indexed names as shown in
the branch headers (e.g. 'acme-api'), NOT by shorthand from the
user's question (e.g. 'mgmtapi'). The user may have used an ambiguous
abbreviation; your answer must be unambiguous."""


_planner_llm = llm("agent", temperature=0, streaming=False)
_synth_llm = llm("agent", temperature=0)


def planner(state: ResearchState) -> dict:
    plan = state.get("query_plan") or {}
    scope: list[str] = plan.get("repo_scope") or []
    repos = scope or available_repos(refresh=True)
    intent = plan.get("intent")

    # Comparison / cross-repo: one branch per scoped repo, repo-pinned.
    # Search with the plan's decomposed queries (stripped of compare/
    # repo-name noise), not the raw question — otherwise "compare auth
    # in A and B" embeds toward audit/report files, not auth code.
    if intent in ("compare", "cross_repo_relationship") and len(repos) > 1:
        sq = (plan.get("search_queries") or [state["query"]])[0]
        return {"sub_questions": [
            {"q": sq, "repo": r, "kind": "code"}
            for r in repos[:MAX_SUBQUESTIONS]
        ]}

    # If query_analysis already decomposed the question, reuse that
    # instead of paying for a second planner call. Repo assignment:
    # zip when counts match; otherwise pin to the SINGLE scope repo if
    # there is one, else leave None and let research_one fall back to
    # the full scope filter.
    pre = plan.get("search_queries") or []
    if pre and len(pre) > 1:
        subs = []
        for i, q in enumerate(pre[:MAX_SUBQUESTIONS]):
            if len(scope) == len(pre):
                r = scope[i]
            elif len(scope) == 1:
                r = scope[0]
            else:
                r = None
            subs.append({"q": q, "repo": r, "kind": "code"})
        return {"sub_questions": subs}

    sys = _PLANNER_SYSTEM.format(maxn=MAX_SUBQUESTIONS, repos=", ".join(repos))
    resp = _planner_llm.invoke([
        SystemMessage(content=sys),
        HumanMessage(content=state["query"]),
    ])
    text = resp.content if isinstance(resp.content, str) else str(resp.content)

    subs: list[dict] = []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
            for item in parsed[:MAX_SUBQUESTIONS]:
                if not isinstance(item, dict) or "q" not in item:
                    continue
                repo = item.get("repo")
                if repo and repo not in repos:
                    repo = None
                subs.append({
                    "q": str(item["q"]),
                    "repo": repo,
                    "kind": item.get("kind", "code") if item.get("kind") in ("code", "docs") else "code",
                })
        except json.JSONDecodeError:
            pass

    if not subs:
        # Fallback: one sub-question per repo with the original query.
        subs = [{"q": state["query"], "repo": r, "kind": "code"} for r in repos[:MAX_SUBQUESTIONS]]

    return {"sub_questions": subs}


def fan_out(state: ResearchState) -> list[Send]:
    """Conditional-edge function returning Send objects → parallel dispatch."""
    scope = (state.get("query_plan") or {}).get("repo_scope") or None
    return [
        Send("research_one", {"sub": sq, "query": state["query"], "scope": scope})
        for sq in state.get("sub_questions", [])
    ]


def research_one(state: dict) -> dict:
    """One parallel branch: hybrid search + rerank for a single sub-question.
    Returns {"findings": [...]} which the operator.add reducer merges."""
    sub = state["sub"]
    q = sub["q"]
    repo = sub.get("repo")
    kind = sub.get("kind", "code")

    scope = state.get("scope") or None
    if kind == "docs":
        candidates = hybrid_search(q, top_k=15, collection=DOCS_COLLECTION)
    else:
        candidates = hybrid_search(q, top_k=15, repo=repo or scope)

    chunks = rerank(q, candidates, top_k=PER_BRANCH_TOP_K) if candidates else []

    return {"findings": [{
        "sub_question": q,
        "repo": repo,
        "kind": kind,
        "chunks": [
            {
                "file": (f"{c.get('repo','')}/{c.get('filepath','')}"
                         f":{c.get('start_line','')}").strip("/:")
                        + (f" p.{c['page_number']}" if c.get("page_number") else ""),
                "symbol": c.get("symbol_name", ""),
                "text": c.get("text", "")[:800],
                "score": round(c.get("rerank_score", 0.0), 3),
            }
            for c in chunks
        ],
    }]}


_FOOTNOTE_RE = re.compile(r"^\[SOURCE_(\d+)\]:\s*`?([^`\n]+?)`?\s*$", re.MULTILINE)


def synthesize(state: ResearchState) -> dict:
    findings = state.get("findings", [])
    if not findings:
        return {"answer": "No findings — research branches returned nothing.",
                "citations": []}

    blocks = []
    for i, f in enumerate(findings):
        head = f"### Branch {i+1}: {f['sub_question']}"
        if f.get("repo"):
            head += f" (repo={f['repo']})"
        chunk_lines = []
        for c in f["chunks"]:
            chunk_lines.append(f"- {c['file']} {c.get('symbol','')}\n  {c['text'][:400]}")
        blocks.append(head + "\n" + ("\n".join(chunk_lines) if chunk_lines else "_(no results)_"))

    context = "\n\n".join(blocks)

    scope = (state.get("query_plan") or {}).get("repo_scope") or []
    sys = _SYNTH_SYSTEM
    if scope:
        sys += f"\n\nRepositories in scope (use these exact names): {', '.join(scope)}."
    sys += "\n\n" + verbosity_instruction(verbosity_resolve(state))
    resp = _synth_llm.invoke([
        SystemMessage(content=sys),
        HumanMessage(content=f"Question: {state['query']}\n\nFindings:\n{context}"),
    ])
    answer = resp.content if isinstance(resp.content, str) else str(resp.content)

    citations = [
        {"ref": f"SOURCE_{n}", "file": path.strip(), "symbol": "", "snippet": ""}
        for n, path in _FOOTNOTE_RE.findall(answer)
    ]

    return {"answer": answer, "citations": citations}


def build_deep_research():
    g = StateGraph(ResearchState)
    g.add_node("planner", planner)
    g.add_node("research_one", research_one)
    g.add_node("synthesize", synthesize)

    g.add_edge(START, "planner")
    g.add_conditional_edges("planner", fan_out, ["research_one"])
    g.add_edge("research_one", "synthesize")
    g.add_edge("synthesize", END)

    return g.compile()


_app = None


def get_deep_research():
    global _app
    if _app is None:
        _app = build_deep_research()
    return _app
