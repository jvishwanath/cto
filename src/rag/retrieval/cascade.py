"""
Tiered retrieval cascade: code → local docs → Confluence → (future:
JIRA, logs). Each tier runs hybrid_search + rerank; the cascade
early-exits when a tier yields ≥ `min_strong` chunks above
`strong_threshold`. Otherwise it carries the best-so-far forward and
falls through to the next tier. The final result is the merged
top-k across visited tiers, re-ranked once more for coherence.

This is the default for simple_rag and exposed as a `search` agent
tool so the LLM doesn't have to know which collection to query.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from .hybrid import hybrid_search, COLLECTION as CODE_COLLECTION
from .reranker import rerank
from ..ingest.docs import DOCS_COLLECTION

log = logging.getLogger(__name__)

STRONG_THRESHOLD = 0.30   # same as output_guard.ABSTAIN_THRESHOLD
MIN_STRONG = 2            # early-exit when this many chunks ≥ threshold
PER_TIER_CANDIDATES = 25
PER_TIER_RERANK = 8


@dataclass
class Tier:
    name: str
    fetch: Callable[[str, list[str] | None], list[dict]]
    enabled: bool = True


@dataclass
class CascadeResult:
    chunks: list[dict]
    visited: list[str] = field(default_factory=list)
    stopped_at: str | None = None
    per_tier: dict = field(default_factory=dict)


def _code(query: str, scope: list[str] | None) -> list[dict]:
    return hybrid_search(query, top_k=PER_TIER_CANDIDATES,
                          collection=CODE_COLLECTION, repo=scope)


def _local_docs(query: str, scope: list[str] | None) -> list[dict]:
    cands = hybrid_search(query, top_k=PER_TIER_CANDIDATES,
                          collection=DOCS_COLLECTION, repo=scope)
    return [c for c in cands if c.get("source") != "confluence"]


def _confluence(query: str, scope: list[str] | None) -> list[dict]:
    # Confluence pages aren't repo-keyed; the relates_to filter is a
    # weak signal. First try with the should-filter (relates_to),
    # then fall back to unscoped if that returns nothing.
    cands = hybrid_search(query, top_k=PER_TIER_CANDIDATES,
                          collection=DOCS_COLLECTION, repo=scope,
                          source="confluence")
    if not cands and scope:
        cands = hybrid_search(query, top_k=PER_TIER_CANDIDATES,
                              collection=DOCS_COLLECTION,
                              source="confluence")
    return cands


# JIRA / logs / metrics tiers slot in here later.
_TIERS = [
    Tier("code", _code),
    Tier("local_docs", _local_docs),
    Tier("confluence", _confluence),
]


def cascade_search(
    query: str,
    scope: list[str] | str | None = None,
    *,
    top_k: int = 8,
    min_strong: int = MIN_STRONG,
    strong_threshold: float = STRONG_THRESHOLD,
    tiers: list[Tier] | None = None,
) -> CascadeResult:
    if isinstance(scope, str):
        scope = [scope]
    tiers = tiers or _TIERS

    pool: list[dict] = []
    seen_ids: set = set()
    res = CascadeResult(chunks=[])

    for tier in tiers:
        if not tier.enabled:
            continue
        try:
            cands = tier.fetch(query, scope)
        except Exception as e:
            log.warning("cascade[%s] fetch failed: %s", tier.name, e)
            res.per_tier[tier.name] = {"error": str(e)}
            continue

        ranked = rerank(query, cands, top_k=PER_TIER_RERANK) if cands else []
        for c in ranked:
            c.setdefault("tier", tier.name)
        n_strong = sum(
            1 for c in ranked
            if (c.get("rerank_score") if "rerank_score" in c
                else c.get("score", 0.0)) >= strong_threshold
        )
        res.visited.append(tier.name)
        res.per_tier[tier.name] = {
            "candidates": len(cands), "ranked": len(ranked),
            "strong": n_strong,
            "top_score": round(max(
                (c.get("rerank_score", c.get("score", 0.0)) for c in ranked),
                default=0.0), 3),
        }

        for c in ranked:
            key = (c.get("filepath") or c.get("file"),
                   c.get("chunk_idx"), c.get("start_line"))
            if key in seen_ids:
                continue
            seen_ids.add(key)
            pool.append(c)

        if n_strong >= min_strong:
            res.stopped_at = tier.name
            break

    # Final merge-rerank across whatever tiers we visited.
    res.chunks = rerank(query, pool, top_k=top_k) if pool else []
    log.info("cascade(%r) visited=%s stopped=%s → %d chunks",
             query[:50], res.visited, res.stopped_at, len(res.chunks))
    return res
