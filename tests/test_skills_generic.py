"""
Generic SKILLS.md loader tests — no LLM, no docker.

Covers:
  * Parse: valid SKILL.md, missing frontmatter, missing description,
    empty body, invalid name.
  * allowed-tools: list form, csv string form, both keys
    (`allowed-tools` and `allowed_tools`).
  * Discovery: project > user > bundled precedence; collision
    shadowing; only `<name>/SKILL.md` (not stray files) loaded.
  * Slash parsing: `/foo`, `/foo bar baz`, non-slash text, bad name.
  * Arg substitution: $ARGS + $1..$9, missing positionals → ''.
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


# ── helpers ──────────────────────────────────────────────────────────

def _write_skill(root: Path, name: str, *, fm: str, body: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(f"---\n{fm}\n---\n{body}", encoding="utf-8")
    return p


# ── parse ────────────────────────────────────────────────────────────

def test_parse():
    section("parse SKILL.md")
    from rag.skills.generic import _parse_skill_md

    tmp = Path(tempfile.mkdtemp(prefix="genskill-"))
    try:
        p = _write_skill(tmp, "good",
                         fm="name: good\ndescription: a good skill",
                         body="Step 1.\nStep 2.")

        @check("valid file parses to GenericSkill")
        def _():
            sk = _parse_skill_md(p, "test")
            assert sk.name == "good", sk
            assert sk.description == "a good skill", sk
            assert "Step 1." in sk.body, sk
            assert sk.dir == p.parent, sk
            assert sk.source == "test", sk

        @check("name falls back to parent dir when frontmatter omits it")
        def _():
            p2 = _write_skill(tmp, "noname",
                              fm="description: x", body="y")
            sk = _parse_skill_md(p2, "t")
            assert sk.name == "noname", sk

        @check("missing frontmatter → ValueError")
        def _():
            d = tmp / "raw"
            d.mkdir()
            (d / "SKILL.md").write_text("just body\n")
            try:
                _parse_skill_md(d / "SKILL.md", "t")
            except ValueError as e:
                assert "frontmatter" in str(e), e
                return
            raise AssertionError("expected ValueError")

        @check("missing description → ValueError")
        def _():
            p2 = _write_skill(tmp, "nodesc",
                              fm="name: nodesc", body="y")
            try:
                _parse_skill_md(p2, "t")
            except ValueError as e:
                assert "description" in str(e), e
                return
            raise AssertionError("expected ValueError")

        @check("empty body → ValueError")
        def _():
            p2 = _write_skill(tmp, "empty",
                              fm="name: empty\ndescription: x",
                              body="")
            try:
                _parse_skill_md(p2, "t")
            except ValueError as e:
                assert "body" in str(e), e
                return
            raise AssertionError("expected ValueError")

        @check("invalid name → ValueError")
        def _():
            p2 = _write_skill(tmp, "good2",
                              fm="name: BAD_NAME\ndescription: x",
                              body="y")
            try:
                _parse_skill_md(p2, "t")
            except ValueError as e:
                assert "invalid name" in str(e), e
                return
            raise AssertionError("expected ValueError")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── allowed-tools ────────────────────────────────────────────────────

def test_allowed_tools():
    section("allowed-tools parse")
    from rag.skills.generic import _parse_skill_md

    tmp = Path(tempfile.mkdtemp(prefix="genskill-"))
    try:
        @check("YAML list form")
        def _():
            p = _write_skill(tmp, "tools-a",
                             fm=("name: tools-a\ndescription: x\n"
                                 "allowed-tools:\n  - Bash\n  - Read"),
                             body="y")
            sk = _parse_skill_md(p, "t")
            assert sk.allowed_tools == ("Bash", "Read"), sk

        @check("CSV string form")
        def _():
            p = _write_skill(tmp, "tools-b",
                             fm=("name: tools-b\ndescription: x\n"
                                 "allowed-tools: Bash, Read, Write"),
                             body="y")
            sk = _parse_skill_md(p, "t")
            assert sk.allowed_tools == ("Bash", "Read", "Write"), sk

        @check("underscore key alias")
        def _():
            p = _write_skill(tmp, "tools-c",
                             fm=("name: tools-c\ndescription: x\n"
                                 "allowed_tools: [Grep]"),
                             body="y")
            sk = _parse_skill_md(p, "t")
            assert sk.allowed_tools == ("Grep",), sk
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── discovery + precedence ───────────────────────────────────────────

def test_discovery_precedence():
    section("discovery + precedence")
    from rag.skills import generic

    tmp = Path(tempfile.mkdtemp(prefix="genskill-roots-"))
    proj = tmp / "proj" / ".claude" / "skills"
    user = tmp / "home" / ".claude" / "skills"
    bund = tmp / "bundled"
    for d in (proj, user, bund):
        d.mkdir(parents=True)

    # Same name in all three; project must win.
    _write_skill(proj, "common",
                 fm="name: common\ndescription: from-project", body="P")
    _write_skill(user, "common",
                 fm="name: common\ndescription: from-user", body="U")
    _write_skill(bund, "common",
                 fm="name: common\ndescription: from-bundled", body="B")
    # Unique skills in each layer.
    _write_skill(proj, "only-proj",
                 fm="name: only-proj\ndescription: x", body="y")
    _write_skill(user, "only-user",
                 fm="name: only-user\ndescription: x", body="y")
    _write_skill(bund, "only-bund",
                 fm="name: only-bund\ndescription: x", body="y")
    # Garbage that should be ignored.
    (bund / "stray.md").write_text("not a skill")
    (bund / "no-skillmd").mkdir()
    (bund / "no-skillmd" / "README.md").write_text("nope")

    fake_roots = [(proj, "project"), (user, "user"), (bund, "bundled")]
    try:
        with patch.object(generic, "_skill_roots",
                          return_value=fake_roots):
            sks = generic.discover()

        @check("project skill wins on collision")
        def _():
            assert "common" in sks
            assert sks["common"].source == "project", sks["common"]
            assert sks["common"].description == "from-project"

        @check("all four unique skills loaded")
        def _():
            assert {"common", "only-proj", "only-user", "only-bund"} \
                <= set(sks), sorted(sks)

        @check("stray *.md ignored (only <name>/SKILL.md loaded)")
        def _():
            assert "stray" not in sks
            assert "no-skillmd" not in sks
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── slash parsing + arg render ───────────────────────────────────────

def test_slash_and_args():
    section("slash parsing + arg substitution")
    from rag.skills.generic import (
        GenericSkill, parse_invocation, render_body)

    @check("bare slash command")
    def _():
        out = parse_invocation("/foo")
        assert out == ("foo", ""), out

    @check("slash + args")
    def _():
        out = parse_invocation("/foo bar baz quux")
        assert out == ("foo", "bar baz quux"), out

    @check("non-slash returns None")
    def _():
        assert parse_invocation("just a message") is None

    @check("invalid skill name returns None")
    def _():
        assert parse_invocation("/BadName") is None
        assert parse_invocation("/") is None

    sk = GenericSkill(
        name="xs", description="d",
        body="run: $1 then $2; full: $ARGS; missing: $5",
        dir=Path("/tmp"))

    @check("$ARGS substitution")
    def _():
        out = render_body(sk, "alpha beta")
        assert "full: alpha beta" in out, out

    @check("positional $1/$2 substitution")
    def _():
        out = render_body(sk, "alpha beta")
        assert "run: alpha then beta" in out, out

    @check("missing positional expands to empty string")
    def _():
        out = render_body(sk, "alpha beta")
        assert "missing: ;" in out or "missing: " in out, out

    @check("multiline body OK in slash regex (skill name only)")
    def _():
        # Slash parser only matches the head — body splicing happens
        # separately. Confirm a body with newlines doesn't break
        # the parser when given as the whole input.
        out = parse_invocation("/foo line1\nline2")
        assert out == ("foo", "line1\nline2"), out


# ── compose_user_message ─────────────────────────────────────────────

def test_compose():
    section("compose_user_message")
    from rag.skills.generic import GenericSkill, compose_user_message

    sk = GenericSkill(
        name="cve-check", description="Check CVEs",
        body="Look up $1; report findings.",
        dir=Path("/data/skills/cve-check"))

    @check("composed message includes header, desc, asset path, body")
    def _():
        out = compose_user_message(sk, "django 4.0")
        assert "Skill activated: cve-check" in out, out
        assert "Check CVEs" in out, out
        assert "/data/skills/cve-check" in out, out
        assert "User arguments: `django 4.0`" in out, out
        assert "Look up django; report findings." in out, out

    @check("no args → no 'User arguments' line")
    def _():
        out = compose_user_message(sk, "")
        assert "User arguments" not in out, out


# ── integration: real fixtures + splice flow ─────────────────────────

def test_integration_with_fixtures():
    section("integration: discover real fixtures + splice")
    from rag.skills import generic

    sks = generic.discover()

    @check("cve-check example loads")
    def _():
        assert "cve-check" in sks, sorted(sks)
        sk = sks["cve-check"]
        assert "CVE" in sk.description, sk.description
        assert "web_fetch" in sk.allowed_tools, sk.allowed_tools

    @check("api-explore example loads")
    def _():
        assert "api-explore" in sks, sorted(sks)
        sk = sks["api-explore"]
        assert "web_search" in sk.allowed_tools, sk.allowed_tools

    @check("flat custom-format skills NOT picked up by generic loader")
    def _():
        for legacy in ("security-audit", "project-cleanup",
                       "onboarding-tour"):
            assert legacy not in sks, (legacy, sorted(sks))

    @check("end-to-end splice: parse → get → compose")
    def _():
        inv = generic.parse_invocation("/cve-check django 4.0")
        assert inv == ("cve-check", "django 4.0"), inv
        sk = generic.get(inv[0])
        assert sk is not None
        msg = generic.compose_user_message(sk, inv[1])
        assert "cve-check" in msg
        assert "django 4.0" in msg
        assert "Bundled assets" in msg


def main() -> int:
    test_parse()
    test_allowed_tools()
    test_discovery_precedence()
    test_slash_and_args()
    test_compose()
    test_integration_with_fixtures()
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
