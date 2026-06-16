"""
Long-term cross-session memory via langgraph PostgresStore.

Unlike the checkpointer (per-thread conversation state), the Store holds
durable facts namespaced by user_id that survive across sessions:
"user works on acme-auth", "user prefers concise answers", etc.
"""

import logging

from langgraph.store.postgres import PostgresStore
from langgraph.store.base import Item

from ..config import POSTGRES_DSN
from ..embed import embed_texts

log = logging.getLogger(__name__)

_store: PostgresStore | None = None
_store_cm = None  # keep CM alive — if it's GC'd the connection closes


def _embed_fn(texts: list[str]) -> list[list[float]]:
    return embed_texts(texts)


def get_store() -> PostgresStore:
    """Lazy singleton. Sets up tables on first use. Vector search
    enabled via the same embedding model as retrieval (1024-dim)."""
    global _store, _store_cm
    if _store is None:
        _store_cm = PostgresStore.from_conn_string(
            POSTGRES_DSN,
            index={"dims": 1024, "embed": _embed_fn},
        )
        # from_conn_string returns a context manager; enter it and keep
        # BOTH the CM and the store alive for the process lifetime.
        # (If the CM is garbage-collected, __exit__ closes the connection.)
        _store = _store_cm.__enter__()
        try:
            _store.setup()
        except Exception as e:
            log.warning("PostgresStore.setup() failed: %s", e)
    return _store


def _ns(user_id: str) -> tuple[str, ...]:
    return ("user", user_id)


def put_memory(user_id: str, key: str, value: dict) -> None:
    get_store().put(_ns(user_id), key, value)


def has_memories(user_id: str) -> bool:
    """Cheap existence check — no embedding call. Used to skip the
    semantic search in load_memories for fresh users (the common
    case), which otherwise pays ~0.5–10s for an empty result."""
    try:
        return bool(get_store().search(_ns(user_id), limit=1))
    except Exception:
        return False


def search_memories(user_id: str, query: str, limit: int = 5) -> list[dict]:
    try:
        if not has_memories(user_id):
            return []
        items: list[Item] = get_store().search(_ns(user_id), query=query, limit=limit)
    except Exception as e:
        log.warning("store.search failed: %s", e)
        return []
    return [
        {"key": it.key, "value": it.value, "score": getattr(it, "score", None)}
        for it in items
    ]


def list_memories(user_id: str, limit: int = 100) -> list[dict]:
    try:
        items: list[Item] = get_store().search(_ns(user_id), limit=limit)
    except Exception as e:
        log.warning("store.search failed: %s", e)
        return []
    return [{"key": it.key, "value": it.value} for it in items]


def delete_memory(user_id: str, key: str) -> None:
    get_store().delete(_ns(user_id), key)
