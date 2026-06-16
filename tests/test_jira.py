"""
JIRA write-tool tests — no live API. Mocks httpx via patch.object on
the singleton client returned by jira._http().

Covers:
  * Tool isolation (writes NOT in regular-graph ALL_TOOLS).
  * jira_add_comment: validation, no-op when unset, confirm denial,
    201 success, 4xx error.
  * jira_list_transitions: no-op when unset, populated list (no
    confirm path).
  * jira_transition_issue: by ID, by name (case-insensitive), unknown
    name lists available, comment payload shape, 204 success, confirm
    denial.
  * Subagent path: build_jira_write_tools(confirm=auto-deny) skips.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import conftest  # noqa: F401

from smoke_test import check, section, results  # noqa: E402


def _mock_client(monkey_dict: dict | None = None):
    """Return a MagicMock that looks like an httpx.Client. Pass an
    optional dict to set initial attrs."""
    c = MagicMock()
    for k, v in (monkey_dict or {}).items():
        setattr(c, k, v)
    return c


def _resp(status: int, body=None, text: str = ""):
    r = MagicMock()
    r.status_code = status
    r.text = text
    if body is not None:
        r.json.return_value = body
    return r


def test_isolation():
    section("JIRA: regular-graph isolation")
    from rag.agents.tools import ALL_TOOLS, TOOLS_BY_NAME

    @check("write tools NOT in TOOLS_BY_NAME")
    def _():
        for n in ("jira_add_comment", "jira_list_transitions",
                  "jira_transition_issue"):
            assert n not in TOOLS_BY_NAME, n

    @check("existing read tools still registered")
    def _():
        assert "jira_lookup" in TOOLS_BY_NAME
        assert "jira_search" in TOOLS_BY_NAME


def test_factory():
    section("JIRA: build_jira_write_tools factory")
    from rag.agents.tools.jira import build_jira_write_tools

    @check("returns 3 tools by expected names")
    def _():
        ts = build_jira_write_tools(confirm=lambda *_: True)
        names = [t.name for t in ts]
        assert names == ["jira_add_comment", "jira_list_transitions",
                         "jira_transition_issue"], names


def test_add_comment():
    section("JIRA: jira_add_comment")
    from rag.agents.tools import jira as J

    add, _lst, _tr = J.build_jira_write_tools(
        confirm=lambda *_: True)

    @check("invalid key → validation message")
    def _():
        out = add.invoke({"ticket": "bogus", "body": "hi"})
        assert "not a valid JIRA key" in out, out

    @check("empty body → refused")
    def _():
        out = add.invoke({"ticket": "PROJ-1", "body": "   "})
        assert "empty" in out, out

    @check("no JIRA_TOKEN → no-op message")
    def _():
        J._client = None
        with patch.object(J, "JIRA_TOKEN", ""):
            out = add.invoke({"ticket": "PROJ-1", "body": "x"})
        assert "not configured" in out, out

    @check("confirm denied → skip message, no POST")
    def _():
        deny_add, _, _ = J.build_jira_write_tools(
            confirm=lambda *_: False)
        fake = _mock_client()
        with patch.object(J, "_http", return_value=fake):
            out = deny_add.invoke({"ticket": "PROJ-1", "body": "x"})
        assert "Skipped by user" in out, out
        assert not fake.post.called

    @check("201 → parsed JSON return")
    def _():
        fake = _mock_client()
        fake.post.return_value = _resp(201, {
            "id": "10001",
            "author": {"displayName": "Alice"},
            "created": "2026-06-10T12:34:56.789+0000",
            "body": "hi there",
        })
        with patch.object(J, "_http", return_value=fake):
            out = add.invoke({"ticket": "proj-1",
                              "body": "hi there"})
        d = json.loads(out)
        assert d["key"] == "PROJ-1"
        assert d["comment_id"] == "10001"
        assert d["author"] == "Alice"
        assert d["created"] == "2026-06-10T12:34:56"
        assert d["body_preview"] == "hi there"
        # Check payload
        call = fake.post.call_args
        assert call.args[0] == "/issue/PROJ-1/comment"
        assert call.kwargs["json"] == {"body": "hi there"}

    @check("403 → error pass-through")
    def _():
        fake = _mock_client()
        fake.post.return_value = _resp(403, text="forbidden")
        with patch.object(J, "_http", return_value=fake):
            out = add.invoke({"ticket": "PROJ-1", "body": "x"})
        assert "[jira 403" in out, out


def test_list_transitions():
    section("JIRA: jira_list_transitions")
    from rag.agents.tools import jira as J

    _add, lst, _tr = J.build_jira_write_tools(
        confirm=lambda *_: True)

    @check("no JIRA_TOKEN → no-op message")
    def _():
        J._client = None
        with patch.object(J, "JIRA_TOKEN", ""):
            out = lst.invoke({"ticket": "PROJ-1"})
        assert "not configured" in out, out

    @check("populated → JSON list of transitions")
    def _():
        fake = _mock_client()
        fake.get.return_value = _resp(200, {"transitions": [
            {"id": "11", "name": "In Progress",
             "to": {"name": "In Progress"}},
            {"id": "21", "name": "Done", "to": {"name": "Done"}},
        ]})
        with patch.object(J, "_http", return_value=fake):
            out = lst.invoke({"ticket": "PROJ-1"})
        d = json.loads(out)
        assert d["key"] == "PROJ-1"
        assert [t["name"] for t in d["transitions"]] == \
            ["In Progress", "Done"]
        assert d["transitions"][0]["to_status"] == "In Progress"

    @check("empty list → returns empty transitions array")
    def _():
        fake = _mock_client()
        fake.get.return_value = _resp(200, {"transitions": []})
        with patch.object(J, "_http", return_value=fake):
            out = lst.invoke({"ticket": "PROJ-1"})
        assert json.loads(out)["transitions"] == []


def test_transition_issue():
    section("JIRA: jira_transition_issue")
    from rag.agents.tools import jira as J

    _add, _lst, trn = J.build_jira_write_tools(
        confirm=lambda *_: True)

    @check("by numeric ID → no GET, POST with {transition.id}")
    def _():
        fake = _mock_client()
        fake.post.return_value = _resp(204)
        with patch.object(J, "_http", return_value=fake):
            out = trn.invoke({"ticket": "PROJ-1",
                              "transition": "21"})
        assert not fake.get.called
        call = fake.post.call_args
        assert call.args[0] == "/issue/PROJ-1/transitions"
        assert call.kwargs["json"] == {"transition": {"id": "21"}}
        d = json.loads(out)
        assert d["transitioned_to"] == "21"
        assert d["comment_added"] is False

    @check("by name (case-insensitive) → resolves ID via GET")
    def _():
        fake = _mock_client()
        fake.get.return_value = _resp(200, {"transitions": [
            {"id": "11", "name": "In Progress"},
            {"id": "21", "name": "Done"},
        ]})
        fake.post.return_value = _resp(204)
        with patch.object(J, "_http", return_value=fake):
            out = trn.invoke({"ticket": "PROJ-1",
                              "transition": "in progress"})
        assert fake.post.call_args.kwargs["json"] == {
            "transition": {"id": "11"}}
        assert json.loads(out)["transitioned_to"] == "In Progress"

    @check("unknown name → error lists available transitions")
    def _():
        fake = _mock_client()
        fake.get.return_value = _resp(200, {"transitions": [
            {"id": "11", "name": "In Progress"}]})
        with patch.object(J, "_http", return_value=fake):
            out = trn.invoke({"ticket": "PROJ-1",
                              "transition": "Closed"})
        assert "No transition matching" in out
        assert "In Progress" in out
        assert not fake.post.called

    @check("with comment → payload includes update.comment.add.body")
    def _():
        fake = _mock_client()
        fake.post.return_value = _resp(204)
        with patch.object(J, "_http", return_value=fake):
            out = trn.invoke({"ticket": "PROJ-1",
                              "transition": "21",
                              "comment": "auto-moved"})
        sent = fake.post.call_args.kwargs["json"]
        assert sent["transition"] == {"id": "21"}
        assert sent["update"] == {
            "comment": [{"add": {"body": "auto-moved"}}]}
        assert json.loads(out)["comment_added"] is True

    @check("confirm denied → skip message, no POST")
    def _():
        _, _, deny_trn = J.build_jira_write_tools(
            confirm=lambda *_: False)
        fake = _mock_client()
        with patch.object(J, "_http", return_value=fake):
            out = deny_trn.invoke({"ticket": "PROJ-1",
                                   "transition": "21"})
        assert "Skipped by user" in out
        assert not fake.post.called

    @check("4xx on POST → error pass-through")
    def _():
        fake = _mock_client()
        fake.post.return_value = _resp(400, text="bad transition")
        with patch.object(J, "_http", return_value=fake):
            out = trn.invoke({"ticket": "PROJ-1",
                              "transition": "21"})
        assert "[jira 400" in out


def test_child_mode():
    section("JIRA: subagent auto-deny via host.build_host_tools")
    from rag.agents.tools.host import build_host_tools

    @check("child_mode=True → write tools auto-deny without POST")
    def _():
        ts = build_host_tools(
            wt=None, allowed_dirs=[__import__("pathlib").Path(".")],
            child_mode=True)
        names = {t.name: t for t in ts}
        assert "jira_add_comment" in names
        from rag.agents.tools import jira as J
        fake = _mock_client()
        with patch.object(J, "_http", return_value=fake):
            out = names["jira_add_comment"].invoke({
                "ticket": "PROJ-1", "body": "hi"})
        assert "Skipped by user" in out
        assert not fake.post.called


def main() -> int:
    test_isolation()
    test_factory()
    test_add_comment()
    test_list_transitions()
    test_transition_issue()
    test_child_mode()

    fails = [r for r in results if not r[1]]
    print("\n" + "─" * 60)
    if fails:
        print(f"\033[91m✗ {len(fails)}/{len(results)} failed\033[0m  "
              f"({len(results) - len(fails)} passed)")
        for n, _, d, _ in fails:
            print(f"  \033[91m✗\033[0m {n}: {d}")
        return 1
    print(f"\033[92m✓ {len(results)}/{len(results)} passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
