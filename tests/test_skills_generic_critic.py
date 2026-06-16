"""
Adversarial / realistic tests for the generic SKILLS.md loader and
the superdev splice path.

Pre-known issues we hunt for here:
  * YAML edge cases: description starting with "Use when:" (colon
    needs quoting), CRLF line endings, BOM, trailing-whitespace
    after `---` fences.
  * allowed-tools with Anthropic-convention paren scopes
    (`Bash(git status:*)`, `WebFetch(domain:example.com)`).
  * Body containing markdown code fences (must survive verbatim).
  * Bundled scripts: absolute path injection in composed message
    must point to a real, readable dir.
  * maybe_splice: HumanMessage at tail / non-HumanMessage at tail /
    empty list / non-existent skill / mid-conversation invocation
    (only the LAST user message is spliced).
  * Tool-mapping hints: only relevant tools mentioned when
    allowed-tools is declared.
  * Splice idempotency: re-running maybe_splice on already-spliced
    messages does not double-wrap.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LITELLM_API_KEY", "sk-test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

import conftest  # noqa: F401

from smoke_test import check, section, results  # noqa: E402


def _write(root: Path, name: str, *, fm: str, body: str,
           encoding: str = "utf-8", newline: str = "\n") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    text = f"---{newline}{fm}{newline}---{newline}{body}"
    p.write_bytes(text.encode(encoding))
    return p


# ── YAML edge cases ─────────────────────────────────────────────────

def test_yaml_edge_cases():
    section("YAML frontmatter edge cases")
    from rag.skills.generic import _parse_skill_md

    tmp = Path(tempfile.mkdtemp(prefix="critic-yaml-"))
    try:
        @check("description with leading 'Use when:' (quoted)")
        def _():
            p = _write(tmp, "uw1",
                       fm=('name: uw1\n'
                           'description: "Use when: investigating an API"'),
                       body="step")
            sk = _parse_skill_md(p, "t")
            assert sk.description == "Use when: investigating an API"

        @check("description WITHOUT quotes — YAML breaks if it has ':'")
        def _():
            # This is a known YAML pitfall. Loader should EITHER parse
            # cleanly OR raise a useful error — but NOT silently drop.
            p = _write(tmp, "uw2",
                       fm=("name: uw2\n"
                           "description: Use when: investigating an API"),
                       body="step")
            try:
                sk = _parse_skill_md(p, "t")
                # If parsed, description may be truncated/mangled; that's
                # acceptable as long as it's non-empty.
                assert sk.description, "description ended up empty"
            except Exception as e:
                # Loader raises — that's also fine (visible failure).
                assert "yaml" in str(e).lower() or "frontmatter" in str(e).lower(), e

        @check("CRLF line endings")
        def _():
            p = _write(tmp, "crlf",
                       fm="name: crlf\ndescription: x",
                       body="line1\nline2",
                       newline="\r\n")
            sk = _parse_skill_md(p, "t")
            assert sk.name == "crlf", sk
            assert "line1" in sk.body, sk

        @check("UTF-8 BOM at start of file")
        def _():
            p = _write(tmp, "bom",
                       fm="name: bom\ndescription: x",
                       body="step",
                       encoding="utf-8-sig")
            try:
                sk = _parse_skill_md(p, "t")
                assert sk.name == "bom", sk
            except ValueError as e:
                # Known limitation: BOM breaks the leading `---`
                # regex. Surface as a clear failure rather than a
                # crash.
                assert "frontmatter" in str(e), e

        @check("trailing whitespace after `---` fence")
        def _():
            # Some editors add trailing spaces.
            p = tmp / "tail" / "SKILL.md"
            p.parent.mkdir()
            p.write_text(
                "---   \nname: tail\ndescription: x\n---   \nbody",
                encoding="utf-8")
            sk = _parse_skill_md(p, "t")
            assert sk.name == "tail", sk
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── allowed-tools with Anthropic paren scopes ───────────────────────

def test_paren_scoped_tools():
    section("allowed-tools with paren scopes")
    from rag.skills.generic import _parse_skill_md

    tmp = Path(tempfile.mkdtemp(prefix="critic-tools-"))
    try:
        @check("Bash(git status:*) parses as one value, not split")
        def _():
            p = _write(tmp, "ps1",
                       fm=('name: ps1\ndescription: x\n'
                           'allowed-tools:\n'
                           '  - "Bash(git status:*)"\n'
                           '  - "Read"\n'),
                       body="y")
            sk = _parse_skill_md(p, "t")
            assert sk.allowed_tools == (
                "Bash(git status:*)", "Read"), sk.allowed_tools

        @check("WebFetch(domain:example.com) preserved")
        def _():
            p = _write(tmp, "ps2",
                       fm=('name: ps2\ndescription: x\n'
                           'allowed-tools: ["WebFetch(domain:example.com)"]'),
                       body="y")
            sk = _parse_skill_md(p, "t")
            assert sk.allowed_tools == (
                "WebFetch(domain:example.com)",), sk.allowed_tools

        @check("CSV string with parens — splits on top-level commas only "
               "(KNOWN LIMITATION: comma INSIDE parens splits too)")
        def _():
            # This is a soft expectation: our CSV splitter is naive
            # and WILL split "Bash(a,b)" into two tokens. Test that
            # YAML list form (above) is the recommended path.
            p = _write(tmp, "ps3",
                       fm=('name: ps3\ndescription: x\n'
                           'allowed-tools: "Bash(git status:*), Read"'),
                       body="y")
            sk = _parse_skill_md(p, "t")
            # Document current behavior — naive split.
            assert "Read" in sk.allowed_tools, sk.allowed_tools
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── markdown / code-fence body survival ─────────────────────────────

def test_body_survival():
    section("body with markdown + code fences survives verbatim")
    from rag.skills.generic import _parse_skill_md

    tmp = Path(tempfile.mkdtemp(prefix="critic-body-"))
    try:
        body = '''# Header

Some prose.

```bash
#!/usr/bin/env bash
echo "hello"
```

```python
def f():
    return 1
```

End.'''

        @check("code fences, shebangs, multi-line code preserved")
        def _():
            p = _write(tmp, "bodyrich",
                       fm="name: bodyrich\ndescription: x",
                       body=body)
            sk = _parse_skill_md(p, "t")
            assert "#!/usr/bin/env bash" in sk.body, sk.body
            assert "```python" in sk.body, sk.body
            assert "def f():" in sk.body, sk.body
            assert sk.body.endswith("End."), sk.body[-50:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── bundled assets ──────────────────────────────────────────────────

def test_bundled_assets_path():
    section("bundled assets path in composed message")
    from rag.skills.generic import (
        _parse_skill_md, compose_user_message)

    tmp = Path(tempfile.mkdtemp(prefix="critic-assets-"))
    try:
        # Skill with a scripts/ subdir containing a real file.
        d = tmp / "withscripts"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: withscripts\ndescription: x\n---\n"
            "Run `$SKILL_DIR/scripts/foo.sh`.",
            encoding="utf-8")
        scripts = d / "scripts"
        scripts.mkdir()
        (scripts / "foo.sh").write_text(
            "#!/bin/sh\necho ok\n", encoding="utf-8")
        sk = _parse_skill_md(d / "SKILL.md", "t")

        @check("compose injects absolute skill.dir path")
        def _():
            out = compose_user_message(sk, "")
            assert str(sk.dir) in out, out

        @check("absolute path actually exists and contains scripts/")
        def _():
            assert (sk.dir / "scripts" / "foo.sh").is_file(), sk.dir
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── maybe_splice end-to-end ─────────────────────────────────────────

def test_maybe_splice():
    section("maybe_splice — graph-level entry point")
    from langchain_core.messages import AIMessage, HumanMessage
    from rag.skills import generic
    from rag.skills.generic import GenericSkill

    fake = GenericSkill(
        name="demo-skill", description="d",
        body="Do $1.", dir=Path("/tmp/demo-skill"))

    def _fake_get(name):
        return fake if name == "demo-skill" else None

    @check("non-slash message → unchanged + None")
    def _():
        with patch.object(generic, "get", _fake_get):
            msgs = [HumanMessage(content="hello there")]
            out, sk = generic.maybe_splice(msgs)
        assert sk is None
        assert out is msgs or out == msgs

    @check("empty list → unchanged + None")
    def _():
        out, sk = generic.maybe_splice([])
        assert sk is None and out == []

    @check("non-Human last message → unchanged + None")
    def _():
        msgs = [AIMessage(content="I'll do it.")]
        out, sk = generic.maybe_splice(msgs)
        assert sk is None

    @check("unknown skill name → unchanged + None")
    def _():
        with patch.object(generic, "get", _fake_get):
            msgs = [HumanMessage(content="/no-such-skill arg")]
            out, sk = generic.maybe_splice(msgs)
        assert sk is None

    @check("/demo-skill foo bar → spliced + skill returned")
    def _():
        with patch.object(generic, "get", _fake_get):
            msgs = [HumanMessage(content="/demo-skill foo bar")]
            out, sk = generic.maybe_splice(msgs)
        assert sk is fake, sk
        assert out is not msgs, "must return new list"
        assert "Skill activated: demo-skill" in out[-1].content
        assert "Do foo." in out[-1].content, out[-1].content

    @check("only the LAST user message is inspected")
    def _():
        with patch.object(generic, "get", _fake_get):
            msgs = [
                HumanMessage(content="/demo-skill earlier"),
                AIMessage(content="ok"),
                HumanMessage(content="please continue"),
            ]
            out, sk = generic.maybe_splice(msgs)
        # Last message is not a slash — no splice.
        assert sk is None

    @check("splice does NOT recurse: re-running on output is a no-op")
    def _():
        with patch.object(generic, "get", _fake_get):
            msgs = [HumanMessage(content="/demo-skill x")]
            once, sk1 = generic.maybe_splice(msgs)
            twice, sk2 = generic.maybe_splice(once)
        assert sk1 is fake
        # Already-spliced text doesn't start with `/`, so second
        # call returns None — no double-wrap.
        assert sk2 is None
        assert once[-1].content == twice[-1].content


# ── tool-name hint relevance ────────────────────────────────────────

def test_tool_hint_filtering():
    section("tool-name hints scoped to declared allowed-tools")
    from rag.skills.generic import GenericSkill, compose_user_message

    @check("declared tools → only those mentioned")
    def _():
        sk = GenericSkill(
            name="t1", description="d", body="b",
            dir=Path("/tmp"),
            allowed_tools=("Bash", "Read"))
        out = compose_user_message(sk, "")
        assert "Bash → host_shell" in out, out
        assert "Read → host_read" in out, out
        assert "WebFetch" not in out, out
        assert "Glob" not in out, out

    @check("paren-scoped tools still map (basename match)")
    def _():
        sk = GenericSkill(
            name="t2", description="d", body="b",
            dir=Path("/tmp"),
            allowed_tools=("Bash(git status:*)",
                           "WebFetch(domain:example.com)"))
        out = compose_user_message(sk, "")
        assert "Bash → host_shell" in out, out
        assert "WebFetch → web_fetch" in out, out

    @check("no allowed-tools declared → all common mappings shown")
    def _():
        sk = GenericSkill(
            name="t3", description="d", body="b",
            dir=Path("/tmp"))
        out = compose_user_message(sk, "")
        for name in ("Bash", "Read", "Write", "WebFetch", "Task"):
            assert name in out, (name, out)


def main() -> int:
    test_yaml_edge_cases()
    test_paren_scoped_tools()
    test_body_survival()
    test_bundled_assets_path()
    test_maybe_splice()
    test_tool_hint_filtering()
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
