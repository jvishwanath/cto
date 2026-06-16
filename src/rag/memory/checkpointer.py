"""
Checkpointer factory: persists LangGraph state per thread_id.
Uses Postgres in dev (the docker-compose service) and prod alike.
"""

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection

from ..config import POSTGRES_DSN

_saver: PostgresSaver | None = None


def get_checkpointer() -> PostgresSaver:
    """Return a singleton PostgresSaver. Creates tables on first use."""
    global _saver
    if _saver is None:
        conn = Connection.connect(POSTGRES_DSN, autocommit=True)
        _saver = PostgresSaver(conn)
        _saver.setup()
    return _saver
