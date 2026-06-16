"""
Postgres connection + schema management for the code graph.
"""

from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from ..config import POSTGRES_DSN

_SCHEMA_PATH = Path(__file__).parent / "graph_schema.sql"
_conn: psycopg.Connection | None = None


def get_conn() -> psycopg.Connection:
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(POSTGRES_DSN, autocommit=True, row_factory=dict_row)
    return _conn


def setup_schema() -> None:
    sql = _SCHEMA_PATH.read_text()
    with get_conn().cursor() as cur:
        cur.execute(sql)


def reset_graph() -> None:
    """Wipe all symbols and edges (cascades)."""
    with get_conn().cursor() as cur:
        cur.execute("TRUNCATE code_symbols, code_edges RESTART IDENTITY CASCADE")
