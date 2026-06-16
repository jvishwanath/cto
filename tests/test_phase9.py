"""
Phase 9 test suite — job spec/cron, scheduler due/next-fire,
headless ask_user/clarify, hooks (diff_prev, notify), exec gating,
sandbox env_passthrough/mounts.

Usage:
  python3 tests/test_phase9.py
  pytest tests/test_phase9.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

os.environ.setdefault("LITELLM_API_KEY", "sk-test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

import conftest  # noqa: F401

from smoke_test import check, section, results, GREEN, RED, RESET  # noqa: E402


# ════════════════════════════════════════════════════════════════════
# 9-A — Job spec + cron
# ════════════════════════════════════════════════════════════════════

def test_job_spec():
    section("Phase 9-A: job spec + cron")
    from rag.scheduler.jobs import _parse, _cron_next, load_jobs, Job

    @check("_parse: skill job, defaults")
    def _():
        j = _parse({"name": "nightly", "cron": "0 2 * * *",
                    "skill": "security-audit",
                    "repos": ["a", "b"],
                    "on_complete": ["diff_prev", "notify"]})
        assert j.kind == "skill" and j.cron == "0 2 * * *"
        assert j.repos == ("a", "b")
        assert j.enabled and j.timeout_sec == 3600

    @check("_parse: query + interval; repos='*' → ('*',)")
    def _():
        j = _parse({"name": "qjob", "interval_min": 60,
                    "query": "hi", "repos": "*"})
        assert j.kind == "query" and j.interval_min == 60
        assert j.repos == ("*",)

    @check("_parse: exec + env_passthrough + exec_mode")
    def _():
        j = _parse({"name": "ec2", "cron": "0 9 * * *",
                    "exec": "scripts/jobs/x.py",
                    "exec_mode": "sandbox",
                    "env_passthrough": ["AWS_PROFILE"]})
        assert j.kind == "exec"
        assert j.env_passthrough == ("AWS_PROFILE",)

    @check("_parse refuses: no schedule, both schedules, no kind")
    def _():
        for bad in (
            {"name": "x", "skill": "y"},
            {"name": "x", "cron": "0 2 * * *", "interval_min": 5,
             "skill": "y"},
            {"name": "x", "cron": "0 2 * * *"},
            {"name": "x", "cron": "0 2 * * *", "skill": "a",
             "query": "b"},
            {"name": "Bad Name", "cron": "0 2 * * *", "skill": "y"},
            {"name": "x", "cron": "lol", "skill": "y"},
            {"name": "x", "cron": "0 2 * * *", "exec": "a.py",
             "exec_mode": "yolo"},
            {"name": "x", "cron": "0 2 * * *", "skill": "y",
             "on_complete": ["explode"]},
        ):
            try:
                _parse(bad)
                assert False, bad
            except ValueError:
                pass

    @check("_cron_next: '0 2 * * *' from 01:30 → today 02:00")
    def _():
        t = datetime(2026, 6, 6, 1, 30, tzinfo=timezone.utc)
        n = _cron_next("0 2 * * *", t)
        assert (n.hour, n.minute) == (2, 0) and n.date() == t.date()

    @check("_cron_next: '0 2 * * *' from 02:00 → tomorrow 02:00")
    def _():
        t = datetime(2026, 6, 6, 2, 0, tzinfo=timezone.utc)
        n = _cron_next("0 2 * * *", t)
        assert n.date() == (t + timedelta(days=1)).date()

    @check("_cron_next: '*/15 * * * *' steps")
    def _():
        t = datetime(2026, 6, 6, 10, 7, tzinfo=timezone.utc)
        n = _cron_next("*/15 * * * *", t)
        assert n.minute == 15 and n.hour == 10

    @check("_cron_next: '0 6 * * 0' (Mon) from a Wed")
    def _():
        t = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)  # Wed
        n = _cron_next("0 6 * * 0", t)
        assert n.weekday() == 0 and n.hour == 6

    @check("Job.is_due: interval — never run → due")
    def _():
        j = Job(name="x", interval_min=60, query="q")
        now = datetime(2026, 6, 6, 10, 0, tzinfo=timezone.utc)
        assert j.is_due(now, None)
        assert not j.is_due(now, now - timedelta(minutes=30))
        assert j.is_due(now, now - timedelta(minutes=61))

    @check("Job.is_due: cron — fire occurred in (last, now]")
    def _():
        j = Job(name="x", cron="0 2 * * *", skill="s")
        now = datetime(2026, 6, 6, 2, 5, tzinfo=timezone.utc)
        assert j.is_due(now, now - timedelta(hours=3))
        assert not j.is_due(now, now - timedelta(minutes=2))
        assert not j.is_due(now, None) is False  # never-run → True

    @check("Job.next_fire: interval anchored on last")
    def _():
        j = Job(name="x", interval_min=30, query="q")
        now = datetime(2026, 6, 6, 10, 0, tzinfo=timezone.utc)
        last = now - timedelta(minutes=10)
        assert j.next_fire(now, last) == last + timedelta(minutes=30)

    @check("load_jobs: fail-soft on bad entry; example parses")
    def _():
        ex = Path(__file__).parents[1] / "data" / "schedules.yaml.example"
        jobs = load_jobs(ex)
        names = {j.name for j in jobs}
        assert {"nightly-secaudit", "weekly-deps",
                "hourly-question", "daily-ec2-cost"} <= names
        # bad doc → []
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                          delete=False) as f:
            f.write("jobs:\n  - {name: BAD, skill: x}\n"
                    "  - {name: ok, interval_min: 5, query: q}\n")
            p = Path(f.name)
        try:
            js = load_jobs(p)
            assert [j.name for j in js] == ["ok"]
        finally:
            p.unlink()


# ════════════════════════════════════════════════════════════════════
# 9-B — Scheduler dispatch + exec gating + sandbox creds
# ════════════════════════════════════════════════════════════════════

class _FakeConn:
    """Minimal in-memory Postgres stand-in for scheduled_runs."""
    rows: list[dict] = []
    next_id = 1

    class _Cur:
        def __init__(self, parent): self.p = parent; self.r = None
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, args=()):
            s = " ".join(sql.split())
            if s.startswith("CREATE"):
                return
            if s.startswith("INSERT"):
                row = {"id": _FakeConn.next_id, "job": args[0],
                       "repo": args[1], "status": "running",
                       "started_at": datetime.now(timezone.utc),
                       "finished_at": None, "report_path": None,
                       "summary": None, "error": None}
                _FakeConn.next_id += 1
                _FakeConn.rows.append(row)
                self.r = [{"id": row["id"]}]
            elif s.startswith("UPDATE"):
                st, rp, su, er, rid = args
                for r in _FakeConn.rows:
                    if r["id"] == rid:
                        r.update(status=st, report_path=rp,
                                 summary=su, error=er,
                                 finished_at=datetime.now(
                                     timezone.utc))
            elif "WHERE job =" in s and "id <" in s:
                job, repo, before = args
                self.r = [r for r in reversed(_FakeConn.rows)
                          if r["job"] == job
                          and r["repo"] == repo
                          and r["id"] < before
                          and r["status"] == "ok"
                          and r["report_path"]][:1]
            elif "WHERE job =" in s:
                job = args[0]
                self.r = [r for r in reversed(_FakeConn.rows)
                          if r["job"] == job][:1]
            elif "ORDER BY started_at DESC LIMIT" in s:
                self.r = list(reversed(_FakeConn.rows))[:args[0]]
        def fetchone(self): return (self.r or [None])[0]
        def fetchall(self): return self.r or []

    def cursor(self): return _FakeConn._Cur(self)


def test_scheduler():
    section("Phase 9-B: scheduler dispatch + exec gating")
    _FakeConn.rows.clear(); _FakeConn.next_id = 1

    with patch("rag.scheduler.store.get_conn",
               return_value=_FakeConn()):
        from rag.scheduler.jobs import Job
        from rag.scheduler import runner as R
        from rag.scheduler.store import last_run_at

        @check("_resolve_repos: '*' → available_repos()")
        def _():
            with patch("rag.scheduler.runner.available_repos",
                       return_value=["a", "b"]):
                j = Job(name="x", interval_min=5, skill="s",
                        repos=("*",))
                assert R._resolve_repos(j) == ["a", "b"]
            assert R._resolve_repos(
                Job(name="x", interval_min=5, query="q")) == [None]

        @check("run_job_now (query): graph.invoke called headless")
        def _():
            captured = {}
            class _G:
                def invoke(self, payload, config):
                    captured["payload"] = payload
                    captured["cfg"] = config
                    return {"answer": "Saved: reports/r/x.md"}
            with patch("rag.agents.graph.get_app",
                       return_value=_G()):
                j = Job(name="qjob", interval_min=5,
                        query="hello", repos=("r",))
                out = R.run_job_now(j)
            assert out[0]["status"] == "ok"
            p = captured["payload"]
            assert p["headless"] is True
            assert p["query_plan"]["repo_scope"] == ["r"]
            assert captured["cfg"]["configurable"]["thread_id"] \
                .startswith("sched/qjob/r/")
            # run was recorded
            assert last_run_at("qjob") is not None

        @check("run_job_now (skill): query='run skill <name> on r'")
        def _():
            captured = {}
            class _G:
                def invoke(self, payload, config):
                    captured["q"] = payload["query"]
                    return {"answer": "ok"}
            with patch("rag.agents.graph.get_app",
                       return_value=_G()):
                j = Job(name="sjob", interval_min=5,
                        skill="security-audit", repos=("r",),
                        headless_answers={"PR": "skip"})
                R.run_job_now(j)
            assert captured["q"] == \
                "run skill security-audit on r"

        @check("run_job_now: graph error → status=error, recorded")
        def _():
            class _G:
                def invoke(self, *a, **k):
                    raise RuntimeError("boom")
            with patch("rag.agents.graph.get_app",
                       return_value=_G()):
                j = Job(name="bad", interval_min=5, query="x")
                out = R.run_job_now(j)
            assert out[0]["status"] == "error"
            assert "boom" in out[0]["error"]

        @check("exec_mode=host refused without flag")
        def _():
            j = Job(name="e", interval_min=5,
                    exec="tests/conftest.py", exec_mode="host")
            with patch("rag.scheduler.runner.HOST_EXEC_ENABLED",
                       False):
                out = R.run_job_now(j)
            assert out[0]["status"] == "error"
            assert "CTO_SCHED_HOST_EXEC" in out[0]["error"]

        @check("exec: script outside project root refused")
        def _():
            j = Job(name="e2", interval_min=5,
                    exec="../../../etc/passwd",
                    exec_mode="host")
            with patch("rag.scheduler.runner.HOST_EXEC_ENABLED",
                       True):
                out = R.run_job_now(j)
            assert out[0]["status"] == "error"
            assert "not found under project root" in out[0]["error"]

        @check("SkillScheduler._due / _next_sleep")
        def _():
            now = datetime(2026, 6, 6, 2, 1, tzinfo=timezone.utc)
            jobs = [
                Job(name="a", cron="0 2 * * *", skill="s"),
                Job(name="b", interval_min=60, query="q"),
                Job(name="c", interval_min=60, query="q",
                    enabled=False),
            ]
            with patch("rag.scheduler.runner.last_run_at",
                       side_effect=lambda n: {
                           "a": now - timedelta(hours=23),
                           "b": now - timedelta(minutes=10),
                       }.get(n)):
                s = R.SkillScheduler(jobs)
                due = s._due(now)
                assert [j.name for j in due] == ["a"]
                sl = s._next_sleep(now)
                assert R._TICK_MIN <= sl <= R._TICK_MAX

        @check("SkillScheduler.status() shape")
        def _():
            jobs = [Job(name="a", cron="0 2 * * *", skill="s")]
            with patch("rag.scheduler.runner.last_run_at",
                       return_value=None):
                s = R.SkillScheduler(jobs)
                st = s.status()
            assert st[0]["name"] == "a"
            assert st[0]["kind"] == "skill"
            assert "next_fire" in st[0]

    @check("DockerSandbox.start: env_passthrough + mounts → argv")
    def _():
        from rag.sandbox.docker import DockerSandbox
        captured = {}
        def _fake(args, **kw):
            captured["argv"] = args
            class R: returncode = 0; stderr = b""
            return R()
        tmp = tempfile.mkdtemp()
        try:
            os.environ["FOO_TEST"] = "bar"
            with patch("rag.sandbox.docker._docker", _fake):
                DockerSandbox().start(
                    env_passthrough=("FOO_TEST", "MISSING"),
                    mounts=(f"{tmp}:/creds", "/nope:/x"))
            argv = captured["argv"]
            assert "-e" in argv
            assert f"FOO_TEST=bar" in argv
            assert "MISSING" not in " ".join(argv)
            assert f"{Path(tmp).resolve()}:/creds:ro" in argv
            assert "/nope" not in " ".join(argv)
        finally:
            os.environ.pop("FOO_TEST", None)
            os.rmdir(tmp)


# ════════════════════════════════════════════════════════════════════
# 9-C — Headless mode
# ════════════════════════════════════════════════════════════════════

def test_headless():
    section("Phase 9-C: headless mode")
    from rag.skills.tools import make_ask_user_tool

    @check("ask_user headless: matches headless_answers key")
    def _():
        t = make_ask_user_tool(headless=True, headless_answers={
            "Open a fix PR": "CRITICAL only",
            "install": "yes",
        })
        r = t.invoke({"question":
                      "Found 3 CRITICAL. Open a fix PR?",
                      "options": ["a", "b"]})
        assert r == "CRITICAL only"

    @check("ask_user headless: no match → 'skip'")
    def _():
        t = make_ask_user_tool(headless=True,
                               headless_answers={"foo": "bar"})
        assert t.invoke({"question": "unrelated?"}) == "skip"

    @check("ask_user interactive: still calls interrupt()")
    def _():
        t = make_ask_user_tool(headless=False)
        with patch("rag.skills.tools.interrupt",
                   return_value="user-said") as mi:
            r = t.invoke({"question": "q?"})
        assert r == "user-said" and mi.called

    @check("clarify headless: repo ambiguity → auto 'all', "
           "no interrupt()")
    def _():
        from rag.agents.nodes import clarify as C
        # 'acme' must match a SUBSET of repos to be flagged
        # ambiguous (matches >= total → skipped as non-narrowing).
        repos = ["acme-billing", "acme-mobile", "other-svc"]
        with patch.object(C, "available_repos",
                          return_value=repos), \
             patch("rag.agents.tools._repo.available_repos",
                   return_value=repos), \
             patch.object(C, "interrupt",
                          side_effect=AssertionError(
                              "interrupt() called in headless")):
            st = {"query": "what does acme do",
                  "messages": [], "clarified": {},
                  "query_plan": {}}
            req = C.needs_clarification(st)
            assert req and req["kind"] == "repo", req
            delta = C.clarify({**st, "headless": True})
            scope = (delta.get("query_plan") or {}) \
                .get("repo_scope", [])
            assert "acme-billing" in scope
            assert "acme-mobile" in scope
            assert "other-svc" not in scope


# ════════════════════════════════════════════════════════════════════
# 9-D — Hooks
# ════════════════════════════════════════════════════════════════════

def test_hooks():
    section("Phase 9-D: on_complete hooks")
    from rag.scheduler import hooks as H

    tmp = Path(tempfile.mkdtemp())
    try:
        prev = tmp / "2026-06-05-r.md"
        cur = tmp / "2026-06-06-r.md"
        prev.write_text("# Report\nA\nB\nC\n")
        cur.write_text("# Report\nA\nB2\nC\nD\n")

        @check("diff_prev: writes -delta.md with +/- counts")
        def _():
            with patch("rag.scheduler.hooks.prev_report",
                       return_value={"report_path": str(prev)}):
                out = H.diff_prev("j", "r", 99, str(cur))
            assert out and out.endswith("-delta.md")
            body = Path(out).read_text()
            assert "```diff" in body and "+D" in body
            assert "−" in body or "-1" in body or "+2" in body

        @check("diff_prev: no prior → None")
        def _():
            with patch("rag.scheduler.hooks.prev_report",
                       return_value=None):
                assert H.diff_prev("j", "r", 1, str(cur)) is None

        @check("notify: slack:// without webhook → log stub, True")
        def _():
            with patch.object(H, "SLACK_WEBHOOK_URL", ""):
                assert H.notify("slack://#chan", "t", "b") is True

        @check("notify: log:// always True; unknown scheme False")
        def _():
            assert H.notify("log://x", "t", "b") is True
            assert H.notify("pager://x", "t", "b") is False
            assert H.notify(None, "t", "b") is False
    finally:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)


# ── runner ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_job_spec()
    test_scheduler()
    test_headless()
    test_hooks()

    n_pass = sum(1 for _, ok, *_ in results if ok)
    n = len(results)
    bar = (GREEN if n_pass == n else RED)
    print(f"\n{bar}══ {n_pass}/{n} passed ══{RESET}\n")
    sys.exit(0 if n_pass == n else 1)
