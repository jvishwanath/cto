"""
Long-term memory nodes: load relevant user facts at graph entry,
optionally extract durable facts at graph exit.
"""

import hashlib
import json
import logging
import re
import threading
import time

from langchain_core.messages import HumanMessage

from ..llm import llm
from ..state import AgentState
from ...memory.store import search_memories, put_memory

log = logging.getLogger(__name__)


_EXTRACT_PROMPT = """Given this user query and the assistant's answer,
extract 0-2 DURABLE facts about the user's working context that would
help future sessions (which repo they primarily work on, their role,
recurring areas of interest). Ignore one-off question content.

Return ONLY a JSON array of strings (empty array if nothing durable):
["fact 1", "fact 2"]

Query: {query}
Answer: {answer}"""


def load_memories(state: AgentState) -> dict:
    """Read user_id from state; search Store for memories relevant to
    the current query. Never fail — return empty on any error."""
    user_id = state.get("user_id") or "anon"
    query = state.get("query") or ""
    try:
        memories = search_memories(user_id, query=query, limit=5)
    except Exception:
        memories = []
    return {"user_memories": [m["value"] for m in memories]}


_SKIP_ROUTES = frozenset({"intro", "blocked", "simple", "research"})
_SESSION_PREFIXES = ("ui-", "cli-", "bench-")
# Synthetic HumanMessages injected by other nodes — not user turns.
_SYNTHETIC_PREFIXES = ("[clarification]", "[evaluator]", "[input_guard]")
_SAVE_TIMEOUT = 8.0  # short — best-effort, never delay the answer


def _extract_and_store(user_id: str, query: str, answer: str) -> None:
    """The actual LLM extraction + Store write. Runs in a daemon
    thread so it never delays graph completion. Writes only to the
    PostgresStore (no graph state), so eventual consistency is fine —
    next turn's load_memories simply may not see this fact yet."""
    t0 = time.perf_counter()
    try:
        model = llm("router", streaming=False, temperature=0,
                    request_timeout=_SAVE_TIMEOUT)
        resp = model.invoke(
            _EXTRACT_PROMPT.format(query=query, answer=answer[:2000]))
        text = (resp.content if isinstance(resp.content, str)
                else str(resp.content))
        m = re.search(r"\[.*\]", text, re.DOTALL)
        facts = json.loads(m.group(0)) if m else []
    except Exception as e:
        log.info("[save_memories bg] llm/extract failed (%s): %s",
                 type(e).__name__, e)
        return

    n = 0
    for fact in facts[:2]:
        if not isinstance(fact, str) or len(fact) < 8:
            continue
        key = "fact_" + hashlib.sha1(fact.encode()).hexdigest()[:12]
        try:
            put_memory(user_id, key, {
                "text": fact, "source_query": query, "ts": time.time(),
            })
            n += 1
        except Exception:
            pass
    log.info("[save_memories bg] user=%s saved=%d in %.1fs",
             user_id, n, time.perf_counter() - t0)


def save_memories(state: AgentState) -> dict:
    """After answering, fire-and-forget a background LLM extraction of
    durable user-context facts into the Store. The node itself returns
    in <1ms — nothing downstream depends on the result, so the graph
    completes immediately and the answer reaches the user without
    waiting on this best-effort tail."""
    user_id = state.get("user_id") or "anon"
    query = state.get("query") or ""
    answer = state.get("answer") or ""
    flags = dict(state.get("guard_flags") or {})

    def _skip(why: str) -> dict:
        flags["save_memories"] = f"skip:{why}"
        return {"guard_flags": flags}

    if (not query or not answer or user_id in ("anon", "ui")
            or user_id.startswith(_SESSION_PREFIXES)):
        return _skip("session-user")
    if state.get("route") in _SKIP_ROUTES:
        return _skip(f"route={state.get('route')}")
    # Superdev action turns (host_write/edit/commit/docker_run/…) emit
    # answers like "Committed 7f3e9a on branch X" — pattern-matches as
    # durable user facts and poisons load_memories on every later turn.
    # Only mine READ turns (search/find_callers/grep/etc.) which look
    # like Q&A.
    if (state.get("route") == "superdev"
            and state.get("turn_kind") != "read"):
        return _skip(f"superdev turn_kind={state.get('turn_kind')}")
    # Real user turns only — clarify/evaluator inject synthetic
    # HumanMessages that would otherwise inflate this count and
    # trigger extraction on what is effectively turn 1.
    n_user_turns = sum(
        1 for m in (state.get("messages") or [])
        if isinstance(m, HumanMessage)
        and not (isinstance(m.content, str)
                 and m.content.startswith(_SYNTHETIC_PREFIXES))
    )
    if n_user_turns < 2:
        return _skip("first-turn")

    threading.Thread(
        target=_extract_and_store, args=(user_id, query, answer),
        daemon=True, name=f"save_memories-{user_id}",
    ).start()
    flags["save_memories"] = "deferred"
    return {"guard_flags": flags}
