"""
Summarize node: when conversation history grows past SUMMARIZE_THRESHOLD,
compress everything except the KEEP_RECENT newest messages into a single
SystemMessage so downstream LLM calls don't pay for the full history.

Works with the `add_messages` reducer on AgentState.messages: returning
RemoveMessage(id=...) entries deletes those messages in place, and the
new SystemMessage is appended. Recent messages are left untouched.
"""

import logging

from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, RemoveMessage,
    SystemMessage, ToolMessage,
)

from ..llm import llm
from ..state import AgentState

log = logging.getLogger(__name__)

SUMMARIZE_THRESHOLD = 20
KEEP_RECENT = 6

_PROMPT = """Summarize this conversation history in under 200 words, preserving:
- which files / symbols / repos were discussed
- what was concluded or answered
- any open questions or unresolved threads

Be terse; this summary replaces the raw history for a code-RAG agent.

--- CONVERSATION ---
{transcript}
--- END ---"""


def should_summarize(state: AgentState) -> str:
    """Conditional-edge function: 'summarize' if history is long, else 'skip'."""
    return "summarize" if len(state.get("messages", [])) > SUMMARIZE_THRESHOLD else "skip"


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        )
    return str(content)


def _render(messages: list[BaseMessage]) -> str:
    lines: list[str] = []
    for m in messages:
        if isinstance(m, HumanMessage):
            lines.append(f"Human: {_text(m.content)}")
        elif isinstance(m, AIMessage):
            tc = getattr(m, "tool_calls", None) or []
            if tc:
                names = ", ".join(c.get("name", "?") for c in tc)
                lines.append(f"AI: called {names}")
            if m.content:
                lines.append(f"AI: {_text(m.content)}")
        elif isinstance(m, ToolMessage):
            body = _text(m.content)
            if len(body) > 100:
                body = body[:100] + "…"
            lines.append(f"Tool[{getattr(m, 'name', '') or m.tool_call_id}]: {body}")
        elif isinstance(m, SystemMessage):
            lines.append(f"[prior context] {_text(m.content)}")
        else:
            lines.append(f"{m.type}: {_text(getattr(m, 'content', ''))}")
    return "\n".join(lines)


def _safe_split(messages: list[BaseMessage], keep_recent: int) -> int:
    """Return a boundary index `k` such that messages[:k] can be
    compressed and messages[k:] preserved, WITHOUT severing an
    AIMessage(tool_calls) from its ToolMessage replies. Walks
    backward from the naive boundary until we land just BEFORE an
    AIMessage(tool_calls) (or at index 0). The naive boundary
    `len-keep_recent` is only safe when no tool chain straddles it."""
    n = len(messages)
    if n <= keep_recent:
        return 0
    k = n - keep_recent
    # If the message AT k is a ToolMessage (or AIMessage without
    # tool_calls following a tool chain), walk back to find the
    # AIMessage that initiated the chain and split BEFORE it.
    while k > 0:
        m = messages[k]
        if isinstance(m, ToolMessage):
            k -= 1
            continue
        # If the message at k-1 has unresolved tool_calls (the kept
        # window starts in the middle of a chain), walk back.
        prev = messages[k - 1]
        if (isinstance(prev, AIMessage)
                and getattr(prev, "tool_calls", None)):
            k -= 1
            continue
        break
    return k


def summarize_history(state: AgentState) -> dict:
    """
    Replace old messages with one SystemMessage summary.
    Returns {"messages": [RemoveMessage(...), ..., SystemMessage(...)]}.
    No-op (returns {}) if LLM fails — never break the conversation.
    """
    messages = state.get("messages", [])
    if len(messages) <= KEEP_RECENT:
        return {}

    k = _safe_split(messages, KEEP_RECENT)
    if k <= 0:
        return {}
    old = messages[:k]
    transcript = _render(old)

    try:
        model = llm("fast", streaming=False, temperature=0)
        resp = model.invoke(_PROMPT.format(transcript=transcript))
        summary_text = _text(resp.content).strip()
    except Exception as e:
        log.warning("[summarize] LLM failed, skipping: %s", e)
        return {}

    if not summary_text:
        return {}

    summary_msg = SystemMessage(
        content=f"[conversation summary — earlier turns compressed]\n{summary_text}"
    )

    delta: list[BaseMessage] = [
        RemoveMessage(id=m.id) for m in old if m.id is not None
    ]
    delta.append(summary_msg)

    log.debug("[summarize] compressed %d msgs → 1 summary (kept %d recent)",
              len(old), len(messages) - k)
    return {"messages": delta}
