"""
Job spec (Phase 9-A): `data/schedules.yaml` → list[Job].

A *job* is one of three kinds, mutually exclusive:
  skill:  run a skill (data/skills/*.md) per repo, headless
  query:  invoke the read-only graph with a free-form question
  exec:   run a checked-in script (no LLM) — sandboxed by
          default, host-mode behind CTO_SCHED_HOST_EXEC

Fail-soft per job: a malformed entry logs and is skipped, never
crashes the server.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

SCHEDULES_PATH = Path(os.environ.get(
    "SCHEDULES_PATH",
    str(Path(__file__).parents[3] / "data" / "schedules.yaml")))

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
# Minimal 5-field cron parser — enough for "0 2 * * *" without
# pulling croniter. Each field: '*', 'N', 'N,M,…', '*/N', 'A-B'.
_CRON_RE = re.compile(r"^([\d*,/\-]+\s+){4}[\d*,/\-]+$")


@dataclass(frozen=True)
class Job:
    name: str
    # schedule
    cron: str | None = None
    interval_min: int | None = None
    # action — exactly one of:
    skill: str | None = None
    query: str | None = None
    exec: str | None = None
    # scope
    repos: tuple[str, ...] = ()
    # behaviour
    enabled: bool = True
    timeout_sec: int = 3600
    on_complete: tuple[str, ...] = ()
    notify: str | None = None
    headless_answers: dict[str, str] = field(default_factory=dict)
    # exec-only
    exec_mode: str = "sandbox"            # "sandbox" | "host"
    exec_image: str = "cto-ops:latest"
    env_passthrough: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        return ("skill" if self.skill
                else "exec" if self.exec
                else "query")

    def next_fire(self, after: datetime,
                  last: datetime | None) -> datetime:
        """Earliest fire time strictly after `after`. For
        interval jobs, anchored on `last` (or `after` if never
        run)."""
        if self.cron:
            return _cron_next(self.cron, after)
        step = timedelta(minutes=self.interval_min or 60)
        anchor = last or (after - step)
        n = anchor + step
        return n if n > after else after + step

    def is_due(self, now: datetime, last: datetime | None) -> bool:
        if not self.enabled:
            return False
        if self.cron:
            # Due if a cron fire occurred in (last, now]. With no
            # prior run, fire once now to establish the anchor.
            if last is None:
                return True
            return _cron_next(self.cron, last) <= now
        gap = (now - last).total_seconds() if last else None
        return gap is None or gap >= (self.interval_min or 60) * 60


# ── cron (5-field, minute precision) ──────────────────────────────

def _expand(field: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = max(1, int(s))
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        out.update(range(max(lo, a), min(hi, b) + 1, step))
    return out


def _cron_next(expr: str, after: datetime) -> datetime:
    mi, hr, dom, mon, dow = expr.split()
    M = _expand(mi, 0, 59)
    H = _expand(hr, 0, 23)
    D = _expand(dom, 1, 31)
    Mo = _expand(mon, 1, 12)
    W = _expand(dow, 0, 6)  # 0 = Mon (Python weekday())
    t = (after.replace(second=0, microsecond=0)
         + timedelta(minutes=1))
    for _ in range(366 * 24 * 60):
        if (t.minute in M and t.hour in H and t.month in Mo
                and t.day in D and t.weekday() in W):
            return t
        t += timedelta(minutes=1)
    return after + timedelta(days=1)


# ── loader ─────────────────────────────────────────────────────────

def _parse(d: dict) -> Job:
    name = str(d.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid job name {name!r}")

    cron = d.get("cron")
    interval = d.get("interval_min")
    if bool(cron) == bool(interval):
        raise ValueError(
            f"{name}: exactly one of cron|interval_min required")
    if cron and not _CRON_RE.match(str(cron).strip()):
        raise ValueError(f"{name}: bad cron expr {cron!r}")

    kinds = [k for k in ("skill", "query", "exec") if d.get(k)]
    if len(kinds) != 1:
        raise ValueError(
            f"{name}: exactly one of skill|query|exec required")

    repos = d.get("repos") or []
    if repos == "*":
        repos = ["*"]
    repos = tuple(str(r) for r in (repos if isinstance(repos, list)
                                   else [repos]))

    on_c = tuple(str(x) for x in (d.get("on_complete") or []))
    bad = [x for x in on_c if x not in ("index", "diff_prev", "notify")]
    if bad:
        raise ValueError(f"{name}: unknown on_complete {bad}")

    exec_mode = str(d.get("exec_mode") or "sandbox")
    if exec_mode not in ("sandbox", "host"):
        raise ValueError(f"{name}: exec_mode must be sandbox|host")

    return Job(
        name=name,
        cron=str(cron).strip() if cron else None,
        interval_min=int(interval) if interval else None,
        skill=d.get("skill"),
        query=d.get("query"),
        exec=d.get("exec"),
        repos=repos,
        enabled=bool(d.get("enabled", True)),
        timeout_sec=int(d.get("timeout_sec", 3600)),
        on_complete=on_c,
        notify=d.get("notify"),
        headless_answers=dict(d.get("headless_answers") or {}),
        exec_mode=exec_mode,
        exec_image=str(d.get("exec_image") or "cto-ops:latest"),
        env_passthrough=tuple(
            str(e) for e in (d.get("env_passthrough") or [])),
    )


def load_jobs(path: Path | None = None) -> list[Job]:
    p = path or SCHEDULES_PATH
    if not p.exists():
        return []
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.warning("[scheduler] %s unreadable: %s", p, e)
        return []
    out: list[Job] = []
    for d in (doc.get("jobs") or []):
        try:
            out.append(_parse(d))
        except Exception as e:
            log.warning("[scheduler] job skipped: %s", e)
    log.info("[scheduler] loaded %d job(s) from %s: %s",
             len(out), p.name, [j.name for j in out])
    return out
