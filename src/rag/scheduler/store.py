"""
scheduled_runs Postgres table — one row per (job, repo) fire.
Survives restarts so `is_due()`, `diff_prev`, and `/jobs` have
state. Reuses indexing/graph_db.get_conn().
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..indexing.graph_db import get_conn

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_runs (
    id           SERIAL PRIMARY KEY,
    job          TEXT NOT NULL,
    repo         TEXT,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    status       TEXT NOT NULL DEFAULT 'running',
    report_path  TEXT,
    summary      TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS scheduled_runs_job_idx
    ON scheduled_runs (job, started_at DESC);
"""


def ensure_schema() -> None:
    with get_conn().cursor() as cur:
        cur.execute(_SCHEMA)


def record_run(job: str, repo: str | None) -> int:
    with get_conn().cursor() as cur:
        cur.execute(
            "INSERT INTO scheduled_runs (job, repo, status) "
            "VALUES (%s, %s, 'running') RETURNING id",
            (job, repo))
        return cur.fetchone()["id"]


def finish_run(run_id: int, *, status: str,
               report_path: str | None = None,
               summary: str | None = None,
               error: str | None = None) -> None:
    with get_conn().cursor() as cur:
        cur.execute(
            "UPDATE scheduled_runs SET finished_at = now(), "
            "status = %s, report_path = %s, summary = %s, "
            "error = %s WHERE id = %s",
            (status, report_path,
             (summary or "")[:2000], (error or "")[:2000], run_id))


def last_run(job: str) -> dict | None:
    with get_conn().cursor() as cur:
        cur.execute(
            "SELECT * FROM scheduled_runs WHERE job = %s "
            "ORDER BY started_at DESC LIMIT 1", (job,))
        return cur.fetchone()


def prev_report(job: str, repo: str | None,
                before_id: int) -> dict | None:
    with get_conn().cursor() as cur:
        cur.execute(
            "SELECT * FROM scheduled_runs WHERE job = %s "
            "AND repo IS NOT DISTINCT FROM %s AND id < %s "
            "AND status = 'ok' AND report_path IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (job, repo, before_id))
        return cur.fetchone()


def list_runs(limit: int = 50) -> list[dict]:
    with get_conn().cursor() as cur:
        cur.execute(
            "SELECT * FROM scheduled_runs "
            "ORDER BY started_at DESC LIMIT %s", (limit,))
        return cur.fetchall()


def last_run_at(job: str) -> datetime | None:
    r = last_run(job)
    if not r or not r.get("started_at"):
        return None
    t = r["started_at"]
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
