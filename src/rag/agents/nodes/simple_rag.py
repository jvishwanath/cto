"""
Simple RAG node: Phase 1 pipeline (hybrid search → rerank → generate) in
a single shot. Used for queries the router classifies as 'simple'.
"""

import logging

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from ..llm import llm
from ..state import AgentState
from ..verbosity import instruction as verbosity_instruction, resolve as verbosity_resolve
from ...retrieval import cascade_search

log = logging.getLogger(__name__)

_SYSTEM = """You are a developer assistant. Answer the question using ONLY
the provided code context. Every claim MUST be followed by a citation
[SOURCE_N] referencing the numbered context block. If the context is
insufficient to answer, say so explicitly — do not guess."""

_agent_llm = llm("agent", temperature=0)


def simple_rag(state: AgentState) -> dict:
    query = state["query"]
    scope = (state.get("query_plan") or {}).get("repo_scope") or None

    cas = cascade_search(query, scope=scope, top_k=8)
    chunks = cas.chunks
    log.debug("[simple_rag] cascade visited=%s stopped=%s chunks=%d",
              cas.visited, cas.stopped_at, len(chunks))

    context_parts = []
    for i, c in enumerate(chunks):
        if c.get("source") in ("confluence", "pdf", "markdown", "text"):
            label = c.get("title") or c["filepath"]
            if c.get("section_path"):
                label += f" › {c['section_path']}"
            if c.get("page_number"):
                label += f" p.{c['page_number']}"
            if c.get("url"):
                label += f" <{c['url']}>"
        else:
            label = f"{c['repo']}/{c['filepath']}:{c.get('start_line','')}"
            if c.get("symbol_name"):
                label += f" ({c['symbol_name']})"
        context_parts.append(f"[SOURCE_{i}] ({label})\n{c['text']}")
    context = "\n\n---\n\n".join(context_parts)

    sys = _SYSTEM
    if scope:
        names = ", ".join(scope) if isinstance(scope, list) else str(scope)
        sys += (f"\n\nThe question is scoped to: {names}. Refer to "
                f"repositories by these exact names in your answer — do "
                f"not echo the user's shorthand.")
    sys += "\n\n" + verbosity_instruction(verbosity_resolve(state))
    response = _agent_llm.invoke([
        SystemMessage(content=sys),
        HumanMessage(content=f"Context:\n{context}\n\nQuestion: {query}"),
    ])

    return {
        "messages": [AIMessage(content=response.content)],
        "retrieved_chunks": chunks,
        "answer": response.content,
        "iteration": 1,
    }
