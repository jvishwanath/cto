"""
SkillScheduler (Phase 9-B).

Generalizes `connectors/scheduler.ConfluenceScheduler`: one
daemon thread wakes, computes `_due_jobs()` from
`scheduled_runs` last-fire, and spawns a worker thread per due
job. Each worker invokes the read-only graph headless
(skill/query) or runs a checked-in script (exec), records the
result, and fires `on_complete` hooks. Per-job wall-clock cap
so a hung run can't block the next fire.

`start_if_configured()` is wired into FastAPI lifespan exactly
like the Confluence scheduler.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import HumanMessage

from ..agents.tools._repo import available_repos
from ..config import REPOS_DIR
from ..sandbox.docker import DockerSandbox
from ..skills.tools import REPORTS_DIR
from . import hooks
from .jobs import Job, load_jobs
from .store import (
    ensure_schema, record_run, finish_run, last_run_at,
)

log = logging.getLogger(__name__)

LOG_PATH = Path(os.environ.get(
    "SCHEDULER_LOG",
    str(Path(REPOS_DIR).parent / "logs" / "scheduler.log")))


def _append_log(job: Job, repo: str | None, *, status: str,
                started: datetime, dur: float, summary: str,
                report: str | None, delta: str | None,
                error: str | None) -> None:
    """One human-readable block per fire → data/logs/scheduler.log.
    Postgres `scheduled_runs` is the structured store; this is the
    `tail -f`-able view (server stdout mixes too many sources)."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        mark = {"ok": "✓", "error": "✗"}.get(status, "·")
        ts = started.strftime("%Y-%m-%d %H:%M:%S %Z")
        head = (f"── {ts} ── {mark} {job.name}"
                + (f" · {repo}" if repo else "")
                + f" · {job.kind} · {dur:.1f}s "
                + "─" * max(0, 60 - len(job.name) - len(repo or "")))
        lines = [head,
                 f"  schedule : {job.cron or f'every {job.interval_min}m'}",
                 f"  status   : {status}"]
        if report:
            lines.append(f"  report   : {report}")
        if delta:
            lines.append(f"  delta    : {delta}")
        if error:
            lines.append(f"  error    : {error}")
        body = (summary or "").rstrip()
        if body:
            lines.append("  output   :")
            for ln in body.splitlines()[:40]:
                lines.append(f"    {ln}")
            if len(body.splitlines()) > 40:
                lines.append(f"    … ({len(body.splitlines()) - 40} "
                             f"more lines; full summary in "
                             f"scheduled_runs / report)")
        lines.append("")
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        log.warning("[scheduler] append log failed: %s", e)

SCHED_ENABLED = os.environ.get("CTO_SCHEDULER_ENABLED",
                               "true").lower() == "true"
HOST_EXEC_ENABLED = os.environ.get("CTO_SCHED_HOST_EXEC",
                                   "false").lower() == "true"
_TICK_MIN, _TICK_MAX = 30.0, 600.0


# ── per-kind execution ─────────────────────────────────────────────

def _resolve_repos(job: Job) -> list[str | None]:
    if not job.repos:
        return [None]
    if job.repos == ("*",):
        return list(available_repos()) or [None]
    return list(job.repos)


_REPORT_RE = re.compile(r"reports/[\w./\- ]+\.md")


def _extract_report(answer: str, repo: str | None) -> str | None:
    m = _REPORT_RE.search(answer or "")
    if not m:
        return None
    p = (REPORTS_DIR.parent / m.group(0)).resolve()
    return str(p) if p.exists() else None


def _run_graph(job: Job, repo: str | None,
               thread_id: str) -> tuple[str, str | None]:
    """Invoke the read-only graph headless. Returns
    (answer_summary, report_path)."""
    from ..agents.graph import get_app
    graph = get_app()
    if job.skill:
        q = f"run skill {job.skill}" + (f" on {repo}" if repo else "")
    else:
        q = job.query or ""
    payload = {
        "query": q,
        "user_id": "scheduler",
        "verbosity": "normal",
        "headless": True,
        "headless_answers": dict(job.headless_answers),
        "messages": [HumanMessage(content=q)],
    }
    if repo:
        payload["query_plan"] = {"repo_scope": [repo],
                                 "needs_human": False}
    cfg = {"configurable": {"thread_id": thread_id},
           "recursion_limit": 200}
    final = graph.invoke(payload, config=cfg)
    answer = (final or {}).get("answer", "") or "(no answer)"
    return answer, _extract_report(answer, repo)


def _run_exec(job: Job, thread_id: str) -> tuple[str, str | None]:
    """Run a checked-in script. Sandbox by default; host only
    when CTO_SCHED_HOST_EXEC=true. argv is fixed in YAML — no
    LLM in the loop — so host mode is just cron, not §8's
    'RCE-as-a-feature'."""
    root = Path(REPOS_DIR).parent.parent  # project root
    script = (root / job.exec).resolve()
    if not script.is_relative_to(root) or not script.exists():
        raise ValueError(f"exec: {job.exec!r} not found under "
                         f"project root")
    env = {k: os.environ[k] for k in job.env_passthrough
           if k in os.environ}

    if job.exec_mode == "host":
        if not HOST_EXEC_ENABLED:
            raise RuntimeError(
                "exec_mode: host requires CTO_SCHED_HOST_EXEC=true")
        r = subprocess.run(
            ["python3", str(script)],
            capture_output=True, text=True,
            timeout=job.timeout_sec,
            env={**os.environ, **env}, cwd=str(root))
        out = r.stdout
        if r.returncode != 0:
            raise RuntimeError(
                f"exit {r.returncode}: {r.stderr.strip()[:500]}")
    else:
        if not DockerSandbox.available():
            raise RuntimeError("exec_mode: sandbox needs Docker")
        # Direct docker run (not DockerSandbox.run_shell): exec
        # scripts live under the project root, not data/repos/,
        # and need allowlisted env passthrough.
        argv = [
            "docker", "run", "--rm",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--memory", "1g", "--cpus", "1",
            *sum(([f"-e", f"{k}={v}"] for k, v in env.items()), []),
            "-v", f"{root}:/repo:ro", "-w", "/repo",
            job.exec_image, "timeout", f"{job.timeout_sec}s",
            "python3", f"/repo/{job.exec}",
        ]
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=job.timeout_sec + 30)
        out = r.stdout
        if r.returncode != 0:
            raise RuntimeError(
                f"exit {r.returncode}: {r.stderr.strip()[:500]}")
    return (out or "").strip()[:4000], None


def _exec_one(job: Job, repo: str | None) -> dict:
    started = datetime.now(timezone.utc)
    ts = started.strftime("%Y%m%dT%H%M%S")
    tid = f"sched/{job.name}/{repo or '-'}/{ts}"
    run_id = record_run(job.name, repo)
    t0 = time.perf_counter()
    try:
        if job.kind == "exec":
            summary, report = _run_exec(job, tid)
        else:
            summary, report = _run_graph(job, repo, tid)
        status, err = "ok", None
    except Exception as e:
        summary, report = "", None
        status, err = "error", f"{type(e).__name__}: {e}"
        log.exception("[scheduler] job %s repo=%s failed",
                      job.name, repo)
    finish_run(run_id, status=status, report_path=report,
               summary=summary, error=err)
    dur = time.perf_counter() - t0

    # ── hooks ──────────────────────────────────────────────────────
    delta_path = None
    if status == "ok":
        for h in job.on_complete:
            if h == "diff_prev":
                delta_path = hooks.diff_prev(job.name, repo,
                                             run_id, report)
            elif h == "notify":
                body = summary
                if delta_path and Path(delta_path).exists():
                    body = Path(delta_path).read_text(
                        encoding="utf-8")[:2800]
                hooks.notify(
                    job.notify,
                    f"{job.name}"
                    + (f" · {repo}" if repo else "")
                    + f" · {dur:.0f}s",
                    body)
    elif "notify" in job.on_complete:
        hooks.notify(job.notify,
                     f"{job.name} FAILED" + (f" · {repo}" if repo else ""),
                     err or "(no detail)")

    log.info("[scheduler] %s repo=%s status=%s %.1fs report=%s",
             job.name, repo, status, dur, report)
    _append_log(job, repo, status=status, started=started,
                dur=dur, summary=summary, report=report,
                delta=delta_path, error=err)
    return {"job": job.name, "repo": repo, "status": status,
            "duration_s": dur, "summary": summary,
            "report": report, "delta": delta_path, "error": err}


def run_job_now(job: Job) -> list[dict]:
    """Synchronous fire (used by `make jobs-run` and tests)."""
    return [_exec_one(job, r) for r in _resolve_repos(job)]


def status_rows() -> list[dict]:
    """Shared row builder for /jobs (HTML+JSON), Gradio, CLI."""
    from .store import last_run, last_run_at
    now = datetime.now(timezone.utc)
    rows = []
    for j in load_jobs():
        last = last_run_at(j.name)
        lr = last_run(j.name) or {}
        nf = j.next_fire(now, last)
        dur = "—"
        if lr.get("finished_at") and lr.get("started_at"):
            d = (lr["finished_at"] - lr["started_at"]).total_seconds()
            dur = f"{d:.0f}s"
        rows.append({
            "name": j.name, "kind": j.kind,
            "schedule": j.cron or f"every {j.interval_min}m",
            "enabled": j.enabled,
            "repos": ",".join(j.repos) or "—",
            "last_run": last.strftime("%Y-%m-%d %H:%M")
                        if last else "never",
            "status": lr.get("status") or "—",
            "duration": dur,
            "next_fire": nf.strftime("%Y-%m-%d %H:%M"),
            "in": _humanize((nf - now).total_seconds()),
            "notify": j.notify or "—",
            "summary": (lr.get("summary") or "")[:200],
            "error": lr.get("error"),
        })
    return rows


def _humanize(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 90: return f"{sec}s"
    if sec < 5400: return f"{sec // 60}m"
    if sec < 129600: return f"{sec // 3600}h"
    return f"{sec // 86400}d"


# ── daemon ─────────────────────────────────────────────────────────

class SkillScheduler:
    def __init__(self, jobs: list[Job] | None = None):
        self.jobs = jobs if jobs is not None else load_jobs()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running: set[str] = set()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.jobs)

    def start(self) -> "SkillScheduler":
        if not self.enabled:
            log.info("[scheduler] no jobs in schedules.yaml — disabled")
            return self
        ensure_schema()
        self._thread = threading.Thread(
            target=self._run, name="skill-scheduler", daemon=True)
        self._thread.start()
        log.info("[scheduler] started: %d job(s): %s",
                 len(self.jobs), [j.name for j in self.jobs])
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _due(self, now: datetime) -> list[Job]:
        out = []
        for j in self.jobs:
            if not j.enabled:
                continue
            with self._lock:
                if j.name in self._running:
                    continue
            if j.is_due(now, last_run_at(j.name)):
                out.append(j)
        return out

    def _next_sleep(self, now: datetime) -> float:
        waits = []
        for j in self.jobs:
            if not j.enabled:
                continue
            nf = j.next_fire(now, last_run_at(j.name))
            waits.append(max(0.0, (nf - now).total_seconds()))
        soonest = min(waits, default=_TICK_MAX)
        return max(_TICK_MIN, min(soonest, _TICK_MAX))

    def _fire(self, job: Job) -> None:
        with self._lock:
            self._running.add(job.name)

        def _worker():
            try:
                for repo in _resolve_repos(job):
                    if self._stop.is_set():
                        break
                    done = threading.Event()
                    res: list[dict] = []
                    t = threading.Thread(
                        target=lambda: (
                            res.append(_exec_one(job, repo)),
                            done.set()),
                        daemon=True)
                    t.start()
                    if not done.wait(job.timeout_sec + 120):
                        log.error("[scheduler] %s repo=%s exceeded "
                                  "wall-clock %ds — abandoning "
                                  "(thread left running; graph "
                                  "checkpoint will be reaped)",
                                  job.name, repo, job.timeout_sec)
            finally:
                with self._lock:
                    self._running.discard(job.name)

        threading.Thread(target=_worker, daemon=True,
                         name=f"job-{job.name}").start()

    def _run(self) -> None:
        self._stop.wait(10)  # let infra come up
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            try:
                for j in self._due(now):
                    log.info("[scheduler] firing %s (kind=%s "
                             "repos=%s)", j.name, j.kind,
                             list(j.repos) or "—")
                    self._fire(j)
            except Exception:
                log.exception("[scheduler] tick failed")
            self._stop.wait(self._next_sleep(
                datetime.now(timezone.utc)))

    def status(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        out = []
        for j in self.jobs:
            last = last_run_at(j.name)
            out.append({
                "name": j.name, "kind": j.kind,
                "schedule": j.cron or f"every {j.interval_min}m",
                "enabled": j.enabled,
                "repos": list(j.repos),
                "running": j.name in self._running,
                "last_run": last.isoformat() if last else None,
                "next_fire": j.next_fire(now, last).isoformat(),
            })
        return out


def start_if_configured() -> SkillScheduler | None:
    if not SCHED_ENABLED:
        log.info("[scheduler] CTO_SCHEDULER_ENABLED=false")
        return None
    s = SkillScheduler()
    if not s.enabled:
        return None
    return s.start()
