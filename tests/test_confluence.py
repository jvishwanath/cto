"""
Confluence connector tests — no live API; Postgres/Qdrant only where
the docker-compose stack is up (gracefully skip otherwise).
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import conftest  # noqa: F401  — puts src/ + tests/ on sys.path

from smoke_test import check, section, results  # noqa: E402


# ════════════════════════════════════════════════════════════════════
# storage_to_markdown
# ════════════════════════════════════════════════════════════════════

def test_storage_md():
    section("Phase 5: Confluence storage_to_markdown")
    from rag.connectors.storage_md import storage_to_markdown

    @check("headings + inline formatting")
    def _():
        out = storage_to_markdown(
            "<h2>Auth</h2><p>Uses <strong>JWT</strong> via "
            "<code>JwksClient</code>.</p>"
        )
        assert "## Auth" in out
        assert "**JWT**" in out
        assert "`JwksClient`" in out

    @check("ac:code macro → fenced block with language")
    def _():
        out = storage_to_markdown(
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">java</ac:parameter>'
            '<ac:plain-text-body><![CDATA[String t = req.getHeader("Authorization");]]>'
            '</ac:plain-text-body></ac:structured-macro>'
        )
        assert "```java" in out
        assert 'getHeader("Authorization")' in out
        assert out.count("```") == 2

    @check("info/warning panel → blockquote with title")
    def _():
        out = storage_to_markdown(
            '<ac:structured-macro ac:name="warning">'
            '<ac:parameter ac:name="title">Caution</ac:parameter>'
            '<ac:rich-text-body><p>do not</p></ac:rich-text-body>'
            '</ac:structured-macro>'
        )
        assert "> **Caution**" in out
        assert "> do not" in out

    @check("nested lists")
    def _():
        out = storage_to_markdown(
            "<ul><li>a<ul><li>a1</li></ul></li><li>b</li></ul>"
        )
        assert "- a" in out and "- b" in out
        assert "  - a1" in out

    @check("table → markdown pipe table with header rule")
    def _():
        out = storage_to_markdown(
            "<table><tr><th>K</th><th>V</th></tr>"
            "<tr><td>a</td><td>1</td></tr></table>"
        )
        assert "| K | V |" in out
        assert "|---|---|" in out
        assert "| a | 1 |" in out

    @check("ac:link page-ref + url-ref + body text")
    def _():
        out = storage_to_markdown(
            '<p><ac:link><ri:page ri:content-title="Design Doc"/>'
            '<ac:plain-text-link-body>see design</ac:plain-text-link-body>'
            '</ac:link> and '
            '<ac:link><ri:url ri:value="https://x.test/y"/></ac:link></p>'
        )
        assert "[see design]" in out
        assert "(https://x.test/y)" in out

    @check("task list → checkboxes")
    def _():
        out = storage_to_markdown(
            '<ac:task-list>'
            '<ac:task><ac:task-status>complete</ac:task-status>'
            '<ac:task-body>done</ac:task-body></ac:task>'
            '<ac:task><ac:task-status>incomplete</ac:task-status>'
            '<ac:task-body>todo</ac:task-body></ac:task>'
            '</ac:task-list>'
        )
        assert "- [x] done" in out
        assert "- [ ] todo" in out

    @check("malformed XML → strip-tags fallback (no exception)")
    def _():
        out = storage_to_markdown("<p>broken <b>tag")
        assert "broken" in out and "tag" in out

    @check("toc dropped; expand → heading; jira → marker")
    def _():
        out = storage_to_markdown(
            '<ac:structured-macro ac:name="toc"/>'
            '<ac:structured-macro ac:name="expand">'
            '<ac:parameter ac:name="title">Details</ac:parameter>'
            '<ac:rich-text-body><p>hidden</p></ac:rich-text-body>'
            '</ac:structured-macro>'
            '<ac:structured-macro ac:name="jira">'
            '<ac:parameter ac:name="key">PROJ-1234</ac:parameter>'
            '</ac:structured-macro>'
        )
        assert "toc" not in out.lower()
        assert "### Details" in out and "hidden" in out
        assert "[JIRA: PROJ-1234]" in out


# ════════════════════════════════════════════════════════════════════
# Config
# ════════════════════════════════════════════════════════════════════

def test_config():
    section("Phase 5: Confluence config")
    from rag.connectors import confluence as cf

    @check("missing file → None")
    def _():
        assert cf.load_config(Path("/nonexistent.yaml")) is None

    @check("env expansion + DC auto-detect + spaces parsed")
    def _():
        os.environ["CONFLUENCE_TOKEN"] = "tok-abc"
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                          delete=False) as f:
            f.write(
                "base_url: https://confluence.example.com\n"
                "auth:\n  token: ${CONFLUENCE_TOKEN}\n"
                "spaces:\n  - key: ENG\n  - key: ARCH\n"
                "    label_filter: [public]\n"
                "sync_interval_hours: 12\nverify_ssl: false\n"
            )
            p = f.name
        cfg = cf.load_config(Path(p))
        os.unlink(p)
        assert cfg and cfg.auth_type == "dc"
        assert cfg.token == "tok-abc"
        assert cfg.sync_interval_hours == 12
        assert cfg.verify_ssl is False
        assert [s.key for s in cfg.spaces] == ["ENG", "ARCH"]
        assert cfg.spaces[1].label_filter == ["public"]
        return f"auth={cfg.auth_type} spaces={[s.key for s in cfg.spaces]}"

    @check("cloud auto-detect from atlassian.net")
    def _():
        os.environ["CONFLUENCE_TOKEN"] = "tok"
        os.environ["CONFLUENCE_USER"] = "u@x.test"
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                          delete=False) as f:
            f.write(
                "base_url: https://corp.atlassian.net/wiki\n"
                "auth:\n  token: ${CONFLUENCE_TOKEN}\n"
                "  user: ${CONFLUENCE_USER}\n"
                "spaces:\n  - key: X\n"
            )
            p = f.name
        cfg = cf.load_config(Path(p))
        os.unlink(p)
        assert cfg and cfg.auth_type == "cloud" and cfg.user == "u@x.test"

    @check("missing token → None (with warning)")
    def _():
        os.environ.pop("CONFLUENCE_TOKEN", None)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                          delete=False) as f:
            f.write("base_url: https://c\nauth: {token: '${CONFLUENCE_TOKEN}'}\n"
                    "spaces:\n  - key: X\n")
            p = f.name
        assert cf.load_config(Path(p)) is None
        os.unlink(p)


# ════════════════════════════════════════════════════════════════════
# Client request shape
# ════════════════════════════════════════════════════════════════════

def test_client():
    section("Phase 5: ConfluenceClient request shape")
    from rag.connectors import confluence as cf

    cfg = cf.ConfluenceConfig(
        base_url="https://confluence.example.com",
        auth_type="dc", token="t", user=None,
        spaces=[cf.SpaceConfig(key="ENG")],
        sync_interval_hours=24, verify_ssl=True,
    )
    cfg_labeled = cf.ConfluenceConfig(
        base_url="https://confluence.example.com",
        auth_type="dc", token="t", user=None,
        spaces=[cf.SpaceConfig(key="ENG", label_filter=["pub"])],
        sync_interval_hours=24, verify_ssl=True,
    )

    @check("DC → Bearer header set, no Basic auth")
    def _():
        c = cf.ConfluenceClient(cfg)
        assert c._http.headers.get("Authorization") == "Bearer t"
        assert c._http.auth is None
        assert str(c._http.base_url).rstrip("/") == \
            "https://confluence.example.com/rest/api"
        c.close()

    def _mock_pages(c, *page_lists):
        captured: list[tuple[str, dict]] = []
        responses = []
        for pl in page_lists:
            r = MagicMock(); r.status_code = 200
            r.json.return_value = {"results": pl, "size": len(pl)}
            responses.append(r)
        # Make the last-but-one a full page so pagination triggers.
        if len(responses) > 1:
            responses[0].json.return_value["size"] = cf._PAGE_BATCH
        it = iter(responses)
        def _get(path, params):
            captured.append((path, dict(params)))
            return next(it)
        return captured, _get

    @check("list_pages: no labels → /content endpoint, paginates")
    def _():
        c = cf.ConfluenceClient(cfg)
        captured, getfn = _mock_pages(
            c,
            [{"id": "1", "version": {"number": 3}, "title": "A"}],
            [{"id": "2", "version": {"number": 1}, "title": "B"}],
        )
        with patch.object(c, "_http") as http:
            http.get.side_effect = getfn
            pages = list(c.list_pages(cfg.spaces[0]))
        assert [p["id"] for p in pages] == ["1", "2"]
        assert captured[0][0] == "/content"
        assert captured[0][1]["spaceKey"] == "ENG"
        assert captured[0][1]["status"] == "current"
        assert captured[0][1]["start"] == 0
        assert captured[1][1]["start"] == cf._PAGE_BATCH
        c.close()

    @check("list_pages: with labels → /content/search CQL")
    def _():
        c = cf.ConfluenceClient(cfg_labeled)
        captured, getfn = _mock_pages(
            c, [{"id": "1", "version": {"number": 1}, "title": "A"}])
        with patch.object(c, "_http") as http:
            http.get.side_effect = getfn
            list(c.list_pages(cfg_labeled.spaces[0]))
        assert captured[0][0] == "/content/search"
        cql = captured[0][1]["cql"]
        assert 'space = "ENG"' in cql and 'label in ("pub")' in cql
        assert "status" not in captured[0][1]
        c.close()
        return f"cql={cql!r}"

    @check("list_pages: HTTP error includes response body")
    def _():
        c = cf.ConfluenceClient(cfg)
        with patch.object(c, "_http") as http:
            r = MagicMock()
            r.status_code = 500; r.reason_phrase = "Server Error"
            r.url = "https://x"; r.text = '{"message":"CQL parse error"}'
            r.request = MagicMock()
            http.get.return_value = r
            try:
                list(c.list_pages(cfg.spaces[0]))
                assert False, "should raise"
            except Exception as e:
                assert "CQL parse error" in str(e), str(e)
        c.close()

    @check("get_page: extracts storage + ancestors + url")
    def _():
        c = cf.ConfluenceClient(cfg)
        with patch.object(c, "_http") as http:
            r = MagicMock(); r.status_code = 200; r.json.return_value = {
                "id": "42", "title": "T",
                "version": {"number": 5},
                "space": {"key": "ENG"},
                "ancestors": [{"title": "Root"}, {"title": "Mid"}],
                "body": {"storage": {"value": "<p>x</p>"}},
                "_links": {"webui": "/spaces/ENG/pages/42"},
            }
            http.get.return_value = r
            p = c.get_page("42")
        assert p["version"] == 5 and p["ancestors"] == ["Root", "Mid"]
        assert p["url"].endswith("/spaces/ENG/pages/42")
        c.close()


# ════════════════════════════════════════════════════════════════════
# Sync (version-diff) — Postgres + Qdrant required.
# ════════════════════════════════════════════════════════════════════

def _infra_up() -> bool:
    try:
        from rag.indexing.graph_db import get_conn
        get_conn().execute("SELECT 1")
        from rag.ingest.docs import _client
        _client().get_collections()
        return True
    except Exception:
        return False


def test_sync():
    section("Phase 5: sync_space (version diff)")
    from rag.connectors import confluence as cf

    if not _infra_up():
        @check("infra (Postgres+Qdrant) not up — skipping sync tests")
        def _(): return "skipped"
        return

    cfg = cf.ConfluenceConfig(
        base_url="https://confluence.example.com",
        auth_type="dc", token="t", user=None,
        spaces=[cf.SpaceConfig(key="_TEST")],
        sync_interval_hours=24, verify_ssl=True,
    )

    # Clean prior test state.
    from rag.indexing.graph_db import get_conn
    cf.ensure_schema()
    with get_conn().cursor() as cur:
        cur.execute("DELETE FROM confluence_pages WHERE space_key = '_TEST'")
        cur.execute("DELETE FROM confluence_sync WHERE space_key = '_TEST'")

    PAGE_BODY = (
        "<h2>Auth</h2><p>Uses <code>JwksClient</code>.</p>"
        '<ac:structured-macro ac:name="code">'
        '<ac:parameter ac:name="language">java</ac:parameter>'
        '<ac:plain-text-body><![CDATA[validateToken();]]></ac:plain-text-body>'
        '</ac:structured-macro>'
    )

    def _client(remote: dict[str, int]) -> MagicMock:
        c = MagicMock(spec=cf.ConfluenceClient)
        c.list_pages.return_value = iter([
            {"id": pid, "version": v, "title": f"P{pid}"}
            for pid, v in remote.items()
        ])
        def _get(pid):
            return {"id": pid, "version": remote[pid], "title": f"P{pid}",
                    "space": "_TEST", "ancestors": ["Root"],
                    "storage": PAGE_BODY,
                    "url": f"https://c/_TEST/{pid}"}
        c.get_page.side_effect = _get
        return c

    @check("first sync: all pages new → all indexed")
    def _():
        c = _client({"100": 1, "101": 1, "102": 1})
        with patch.object(cf, "embed_texts",
                          side_effect=lambda ts: [[0.0]*1024 for _ in ts]):
            r = cf.sync_space(c, cfg.spaces[0])
        assert r.seen == 3 and r.changed == 3 and r.deleted == 0, r
        assert r.chunks > 0
        local = cf._local_versions("_TEST")
        assert local == {"100": 1, "101": 1, "102": 1}
        return f"chunks={r.chunks}"

    @check("second sync, no changes → 0 changed, 0 deleted")
    def _():
        c = _client({"100": 1, "101": 1, "102": 1})
        with patch.object(cf, "embed_texts",
                          side_effect=lambda ts: [[0.0]*1024 for _ in ts]):
            r = cf.sync_space(c, cfg.spaces[0])
        assert r.seen == 3 and r.changed == 0 and r.deleted == 0, r
        assert c.get_page.call_count == 0
        return "no-op detected"

    @check("version bump on one page → only that page re-indexed")
    def _():
        c = _client({"100": 1, "101": 2, "102": 1})
        with patch.object(cf, "embed_texts",
                          side_effect=lambda ts: [[0.0]*1024 for _ in ts]):
            r = cf.sync_space(c, cfg.spaces[0])
        assert r.changed == 1 and r.deleted == 0, r
        assert c.get_page.call_args_list[0].args == ("101",)
        assert cf._local_versions("_TEST")["101"] == 2

    @check("page removed remotely → deleted from index + state")
    def _():
        c = _client({"100": 1, "101": 2})  # 102 gone
        with patch.object(cf, "embed_texts",
                          side_effect=lambda ts: [[0.0]*1024 for _ in ts]):
            r = cf.sync_space(c, cfg.spaces[0])
        assert r.seen == 2 and r.changed == 0 and r.deleted == 1, r
        assert "102" not in cf._local_versions("_TEST")

    @check("force=True re-indexes everything regardless of version")
    def _():
        c = _client({"100": 1, "101": 2})
        with patch.object(cf, "embed_texts",
                          side_effect=lambda ts: [[0.0]*1024 for _ in ts]):
            r = cf.sync_space(c, cfg.spaces[0], force=True)
        assert r.changed == 2, r

    @check("list_pages failure → error captured, state untouched")
    def _():
        c = MagicMock(spec=cf.ConfluenceClient)
        c.list_pages.side_effect = RuntimeError("503")
        before = dict(cf._local_versions("_TEST"))
        r = cf.sync_space(c, cfg.spaces[0])
        assert r.error and "503" in r.error
        assert cf._local_versions("_TEST") == before

    @check("sync_status reflects last run")
    def _():
        rows = cf.sync_status()
        row = next(r for r in rows if r["space_key"] == "_TEST")
        assert row["pages_indexed"] == 2
        assert row["last_run"] is not None
        return f"indexed={row['pages_indexed']}"

    @check("chunks landed in 'docs' with confluence payload")
    def _():
        from rag.ingest.docs import _client as q, DOCS_COLLECTION
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        n = q().count(
            DOCS_COLLECTION,
            count_filter=Filter(must=[
                FieldCondition(key="source", match=MatchValue(value="confluence")),
                FieldCondition(key="space", match=MatchValue(value="_TEST")),
            ]),
            exact=True,
        ).count
        assert n > 0
        # cleanup
        cf._delete_page_chunks("_TEST", "100")
        cf._delete_page_chunks("_TEST", "101")
        with get_conn().cursor() as cur:
            cur.execute("DELETE FROM confluence_pages WHERE space_key = '_TEST'")
            cur.execute("DELETE FROM confluence_sync WHERE space_key = '_TEST'")
        return f"{n} chunks in Qdrant"


# ════════════════════════════════════════════════════════════════════
# Scheduler
# ════════════════════════════════════════════════════════════════════

def test_scheduler():
    section("Phase 5: ConfluenceScheduler")
    from rag.connectors import confluence as cf
    from rag.connectors.scheduler import ConfluenceScheduler

    cfg = cf.ConfluenceConfig(
        base_url="https://c", auth_type="dc", token="t", user=None,
        spaces=[cf.SpaceConfig(key="A"), cf.SpaceConfig(key="B")],
        sync_interval_hours=24, verify_ssl=True,
    )

    @check("disabled when no config / no spaces")
    def _():
        assert ConfluenceScheduler(None).enabled is False
        empty = cf.ConfluenceConfig(
            base_url="https://c", auth_type="dc", token="t", user=None,
            spaces=[], sync_interval_hours=24, verify_ssl=True)
        assert ConfluenceScheduler(empty).enabled is False
        assert ConfluenceScheduler(cfg).enabled is True

    @check("_due_spaces: never-synced → all due; recent → none")
    def _():
        s = ConfluenceScheduler(cfg)
        now = datetime.now(timezone.utc)
        with patch("rag.connectors.scheduler.last_sync") as ls:
            ls.return_value = None
            assert {sp.key for sp in s._due_spaces()} == {"A", "B"}
            ls.side_effect = lambda k: {
                "last_run": now - timedelta(hours=1)}
            assert s._due_spaces() == []
            ls.side_effect = lambda k: {
                "last_run": now - timedelta(hours=25)}
            assert {sp.key for sp in s._due_spaces()} == {"A", "B"}

    @check("_next_sleep clamped to [60, 3600]")
    def _():
        s = ConfluenceScheduler(cfg)
        now = datetime.now(timezone.utc)
        with patch("rag.connectors.scheduler.last_sync") as ls:
            ls.side_effect = lambda k: None
            assert s._next_sleep() == 60.0
            ls.side_effect = lambda k: {"last_run": now}
            assert s._next_sleep() == 3600.0
            ls.side_effect = lambda k: {
                "last_run": now - timedelta(hours=23, minutes=58)}
            w = s._next_sleep()
            assert 60.0 <= w <= 200.0, w


def main() -> int:
    test_storage_md()
    test_config()
    test_client()
    test_sync()
    test_scheduler()

    fails = [r for r in results if not r[1]]
    print("\n" + "─" * 60)
    if fails:
        print(f"\033[91m✗ {len(fails)}/{len(results)} failed\033[0m  "
              f"({len(results)-len(fails)} passed)")
        for n, _, d, _ in fails:
            print(f"  \033[91m✗\033[0m {n}: {d}")
        return 1
    print(f"\033[92m✓ {len(results)}/{len(results)} passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
