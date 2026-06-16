"""
Semantic query cache.

Before invoking the LangGraph agent, embed the incoming query and check the
'query_cache' Qdrant collection for a recent, semantically-equivalent question.
If cosine similarity >= threshold and the entry hasn't expired, return the
cached answer + citations and skip the graph entirely.

Dense-only (1024-dim cosine) — no sparse/BM25 needed: we want paraphrase
matching, not lexical overlap.
"""

import logging
import time
import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct,
    Filter, FieldCondition, Range, FilterSelector,
    IsEmptyCondition, MatchValue, PayloadField,
)

from ..config import QDRANT_URL
from ..embed import embed_query

log = logging.getLogger(__name__)

CACHE_COLLECTION = "query_cache"


def seed_history_on_hit(graph, config: dict, query: str,
                        hit: dict) -> None:
    """Write a cache-hit Q+A pair into the checkpointer so the NEXT
    turn in this session has conversation history to refer to.

    A cache hit short-circuits before graph.stream(), so without
    this the checkpointer for a fresh thread stays empty and
    referential follow-ups ('explain that', 'where is it called')
    arrive with has_history=False → query_analysis flags
    needs_human → user gets re-asked for context they just saw.

    update_state() goes through the add_messages reducer, so this
    appends to (not replaces) any prior history."""
    from langchain_core.messages import HumanMessage, AIMessage
    try:
        graph.update_state(config, {
            "messages": [
                HumanMessage(content=query),
                AIMessage(content=hit["answer"]),
            ],
            "query": query,
            "answer": hit["answer"],
            "citations": hit.get("citations") or [],
            "route": "cache",
        })
    except Exception:
        pass

VECTOR_DIM = 1024
CACHE_NS = uuid.UUID("c0ffee00-4f89-41d3-9a0c-0305e82c3302")

_qdrant: QdrantClient | None = None


def _client() -> QdrantClient:
    global _qdrant
    if _qdrant is None:
        _qdrant = QdrantClient(url=QDRANT_URL)
    return _qdrant


def _normalize(query: str, mode: str = "regular") -> str:
    """Mode is prefixed so superdev / regular caches never collide
    (action-turn outputs in superdev would otherwise shadow Q&A
    answers for identical paraphrases)."""
    return f"{mode}::" + " ".join(query.lower().split())


def _point_id(query: str, mode: str = "regular") -> str:
    return str(uuid.uuid5(CACHE_NS, _normalize(query, mode)))


def ensure_cache_collection() -> None:
    """Create 'query_cache' collection (dense-only, 1024-dim cosine) if missing."""
    c = _client()
    if not c.collection_exists(CACHE_COLLECTION):
        c.create_collection(
            collection_name=CACHE_COLLECTION,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )


def cache_lookup(query: str, threshold: float = 0.95,
                 mode: str = "regular") -> dict | None:
    """Embed query, search 'query_cache' with a `mode`-scoped payload
    filter. If best non-expired hit has score >= threshold, return
    {answer, citations, cached_query, score, age_sec}. Else None.

    Mode-scoping prevents cross-mode collisions: a superdev action
    turn that ran `commit(...)` must not surface as the answer to the
    same paraphrase asked in regular Q&A mode."""
    c = _client()
    if not c.collection_exists(CACHE_COLLECTION):
        return None

    vec = embed_query(query)
    now = time.time()

    # Server-side mode filter so wrong-mode entries near the
    # paraphrase cluster don't crowd out the right-mode hit at
    # rank K+1. For mode='regular' OR in an IsEmpty fallback so
    # pre-8.5-G entries (no `mode` field) still match.
    if mode == "regular":
        mode_filter = Filter(should=[
            FieldCondition(key="mode", match=MatchValue(value=mode)),
            IsEmptyCondition(is_empty=PayloadField(key="mode")),
        ])
    else:
        mode_filter = Filter(must=[
            FieldCondition(key="mode", match=MatchValue(value=mode)),
        ])
    res = c.query_points(
        collection_name=CACHE_COLLECTION,
        query=vec,
        limit=3,
        with_payload=True,
        query_filter=mode_filter,
    )

    if not res.points:
        log.debug("[cache_lookup] no points in %s (mode=%s)",
                  CACHE_COLLECTION, mode)
        return None

    for p in res.points:
        payload = p.payload or {}
        if p.score < threshold:
            log.debug("[cache_lookup] best=%.3f < %s (%r)",
                      p.score, threshold, payload.get('query', '')[:50])
            break
        # Server already filtered by mode; just check expiry.
        if payload.get("expires_at", 0.0) < now:
            log.debug("[cache_lookup] expired (age=%.0fs)",
                      now - payload.get('created_at', now))
            continue
        return {
            "answer": payload.get("answer", ""),
            "citations": payload.get("citations") or [],
            "cached_query": payload.get("query", ""),
            "score": float(p.score),
            "age_sec": now - payload.get("created_at", now),
        }
    return None


def store_eligible(
    user_msg: str,
    final_state: dict,
    verbosity: str,
    *,
    was_resume: bool = False,
) -> tuple[str, bool, str]:
    """Decide whether this turn's answer should be cached, and under
    what key. Returns (cache_key, ok, reason_if_not).

    Cache key is the RESOLVED query (post-clarify, post-redaction) so
    ambiguous shorthand never poisons the cache. The 'fresh' gate is
    gone — instead we exclude only answers that are inherently
    context-dependent."""
    from ..agents.verbosity import is_expand_request

    key = (final_state.get("query") or user_msg or "").strip()
    flags = final_state.get("guard_flags") or {}
    route = final_state.get("route")

    if not key:
        return key, False, "empty query"
    if verbosity not in ("normal", "terse"):
        return key, False, f"verbosity={verbosity}"
    if was_resume and not final_state.get("clarified"):
        return key, False, "resume without clarify (mid-turn answer)"
    if is_expand_request(user_msg):
        return key, False, "expand-request (context-only)"
    if route in (None, "blocked", "intro", "skill"):
        return key, False, f"route={route}"
    # Superdev action turns produce non-idempotent side-effects
    # ("Committed sha-abc on branch X"); caching them poisons the
    # next paraphrase. Only read turns survive.
    if route == "superdev" and final_state.get("turn_kind") != "read":
        return key, False, f"superdev turn_kind={final_state.get('turn_kind')}"
    if flags.get("blocked") or flags.get("abstained"):
        return key, False, f"guard {flags}"
    # Onboarding phrasings answered via agent-route (mid-history) must
    # not be cached — they would shadow the dynamic intro response in
    # a fresh session.
    from ..agents.nodes.intro import is_intro_query
    if is_intro_query(user_msg) or is_intro_query(key):
        return key, False, "intro-phrasing"
    return key, True, ""


def cache_store(query: str, answer: str, citations: list | None = None,
                ttl_sec: int = 3600, mode: str = "regular") -> str:
    """Embed query, upsert to 'query_cache' with a `mode`-scoped
    point id and payload. Return point id (UUID5 of mode::query)."""
    ensure_cache_collection()
    now = time.time()
    pid = _point_id(query, mode)
    vec = embed_query(query)
    _client().upsert(
        collection_name=CACHE_COLLECTION,
        points=[PointStruct(
            id=pid,
            vector=vec,
            payload={
                "query": query,
                "answer": answer,
                "citations": citations or [],
                "created_at": now,
                "expires_at": now + ttl_sec,
                "mode": mode,
            },
        )],
        wait=True,
    )
    return pid


def cache_purge_expired() -> int:
    """Delete points where expires_at < now. Return count deleted."""
    c = _client()
    if not c.collection_exists(CACHE_COLLECTION):
        return 0
    now = time.time()
    flt = Filter(must=[FieldCondition(key="expires_at", range=Range(lt=now))])
    n = c.count(CACHE_COLLECTION, count_filter=flt, exact=True).count
    if n:
        c.delete(
            collection_name=CACHE_COLLECTION,
            points_selector=FilterSelector(filter=flt),
            wait=True,
        )
    return n
