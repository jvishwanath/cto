"""
Code graph queries via recursive CTEs over Postgres.
"""

from ..indexing.graph_db import get_conn


def find_symbol(name: str, repo: str | None = None,
                case_sensitive: bool = False) -> list[dict]:
    """All definitions matching `name` (method/function/class), across repos.
    Default match is case-INsensitive — symbol-name casing varies by language
    (camelCase Java, snake_case Python) and LLMs frequently lowercase. Pass
    case_sensitive=True for strict exact match."""
    op = "=" if case_sensitive else "ILIKE"
    sql = f"""
        SELECT id, repo, filepath, name, kind, class_name, fqn, start_line, end_line
        FROM code_symbols
        WHERE name {op} %s
    """
    params: list = [name]
    if repo:
        sql += " AND repo = %s"
        params.append(repo)
    sql += " ORDER BY repo, filepath, start_line"

    with get_conn().cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def find_callers(name: str, depth: int = 2, repo: str | None = None,
                 case_sensitive: bool = False) -> list[dict]:
    """
    Who calls `name`, transitively up to `depth` levels.
    Uses resolved to_symbol when available; falls back to callee_name match
    so external/unresolved targets still surface direct callers.

    Default match is case-INsensitive — symbol-name casing varies by
    language and LLMs frequently lowercase. Pass case_sensitive=True
    for strict exact match (parity with find_symbol).
    """
    op = "=" if case_sensitive else "ILIKE"
    sql = f"""
    WITH RECURSIVE
    targets AS (
        SELECT id FROM code_symbols
        WHERE name {op} %(name)s
          AND kind IN ('method','function','constructor')
          AND (%(repo)s::text IS NULL OR repo = %(repo)s::text)
    ),
    callers AS (
        -- depth 1: direct callers (via resolved edge OR raw name match)
        SELECT e.from_symbol AS sym_id, 1 AS depth,
               ARRAY[e.from_symbol] AS path
        FROM code_edges e
        WHERE e.relation = 'calls'
          AND (e.to_symbol IN (SELECT id FROM targets)
               OR e.callee_name {op} %(name)s)

        UNION

        -- depth N: callers of the callers
        SELECT e.from_symbol, c.depth + 1, c.path || e.from_symbol
        FROM code_edges e
        JOIN callers c ON e.to_symbol = c.sym_id
        WHERE e.relation = 'calls'
          AND c.depth < %(depth)s
          AND NOT e.from_symbol = ANY(c.path)
    )
    SELECT DISTINCT ON (s.id)
           c.depth, s.repo, s.filepath, s.class_name, s.name, s.kind, s.start_line
    FROM callers c
    JOIN code_symbols s ON s.id = c.sym_id
    ORDER BY s.id, c.depth
    """
    with get_conn().cursor() as cur:
        cur.execute(sql, {"name": name, "depth": depth, "repo": repo})
        rows = cur.fetchall()
    return sorted(rows, key=lambda r: (r["depth"], r["repo"], r["filepath"]))


def find_callees(name: str, depth: int = 2, repo: str | None = None,
                 case_sensitive: bool = False) -> list[dict]:
    """
    What does `name` call, transitively down to `depth` levels.
    Includes unresolved (external) callees as leaf rows with repo=NULL.

    Default match is case-INsensitive (parity with find_symbol /
    find_callers). Pass case_sensitive=True for strict match.
    """
    op = "=" if case_sensitive else "ILIKE"
    sql = f"""
    WITH RECURSIVE
    sources AS (
        SELECT id FROM code_symbols
        WHERE name {op} %(name)s
          AND kind IN ('method','function','constructor')
          AND (%(repo)s::text IS NULL OR repo = %(repo)s::text)
    ),
    callees AS (
        SELECT e.id AS edge_id, e.to_symbol, e.callee_name, e.callee_class,
               1 AS depth, ARRAY[e.from_symbol] AS path
        FROM code_edges e
        WHERE e.relation = 'calls'
          AND e.from_symbol IN (SELECT id FROM sources)

        UNION

        SELECT e.id, e.to_symbol, e.callee_name, e.callee_class,
               c.depth + 1, c.path || e.from_symbol
        FROM code_edges e
        JOIN callees c ON e.from_symbol = c.to_symbol
        WHERE e.relation = 'calls'
          AND c.depth < %(depth)s
          AND c.to_symbol IS NOT NULL
          AND NOT e.from_symbol = ANY(c.path)
    )
    SELECT DISTINCT ON (COALESCE(s.id, -c.edge_id))
           c.depth,
           s.repo, s.filepath, s.class_name,
           COALESCE(s.name, c.callee_name) AS name,
           COALESCE(s.kind, 'external')    AS kind,
           c.callee_class                  AS via_class,
           s.start_line
    FROM callees c
    LEFT JOIN code_symbols s ON s.id = c.to_symbol
    ORDER BY COALESCE(s.id, -c.edge_id), c.depth
    """
    with get_conn().cursor() as cur:
        cur.execute(sql, {"name": name, "depth": depth, "repo": repo})
        rows = cur.fetchall()
    return sorted(rows, key=lambda r: (r["depth"], r.get("repo") or "~", r["name"]))


def graph_stats() -> dict:
    with get_conn().cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM code_symbols")
        n_sym = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM code_edges")
        n_edge = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM code_edges WHERE to_symbol IS NOT NULL")
        n_res = cur.fetchone()["n"]
    return {"symbols": n_sym, "edges": n_edge, "resolved": n_res,
            "resolution_rate": round(n_res / n_edge, 3) if n_edge else 0}
