"""
Git history tools — option C (no index, live from .git).

Reads data/repos/<name>/.git directly via subprocess. Always
reflects the working-tree's current HEAD; no sync, no staleness.
JIRA IDs are matched as \\b[A-Z]{2,}-\\d+\\b across all repos.
"""

import json
import re
import subprocess
from pathlib import Path

from langchain_core.tools import tool

from ...config import REPOS_DIR
from ._repo import available_repos, resolve_repo

_JIRA_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
_SEP = "\x1f"
_FMT = _SEP.join(["%H", "%h", "%an", "%aI", "%s", "%b"]) + "\x1e"


def _git_repos(repo: str | None) -> list[tuple[str, Path]]:
    base = Path(REPOS_DIR)
    names = [repo] if repo else available_repos()
    out = []
    for n in names:
        p = base / n
        if (p / ".git").exists() or (p.is_dir()
                                     and (p.resolve() / ".git").exists()):
            out.append((n, p))
    return out


def _run_git(repo_path: Path, args: list[str], timeout: int = 15) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"[timeout after {timeout}s]"
    if r.returncode != 0:
        return f"[git error: {r.stderr.strip()[:200]}]"
    return r.stdout


def _parse_log(out: str, repo: str) -> list[dict]:
    commits = []
    for rec in out.split("\x1e"):
        rec = rec.strip()
        if not rec:
            continue
        parts = rec.split(_SEP)
        if len(parts) < 6:
            continue
        sha, short, author, date, subject, body = parts[:6]
        msg = (subject + ("\n" + body if body.strip() else "")).strip()
        commits.append({
            "repo": repo,
            "sha": short,
            "full_sha": sha,
            "author": author,
            "date": date,
            "message": msg[:500],
            "jira": sorted(set(_JIRA_RE.findall(msg))),
        })
    return commits


# ── Tools ────────────────────────────────────────────────────────────

@tool
def git_log(repo: str | None = None, path: str | None = None,
            grep: str | None = None, author: str | None = None,
            since: str | None = None, limit: int = 20) -> str:
    """Read git commit history for one or all indexed repositories.

    Use this to answer: "what changed recently in X", "who last
    modified <file>", "commits mentioning <keyword>", "what shipped
    since last week". Returns JSON: [{repo, sha, author, date,
    message, jira[]}].

    Args:
        repo: Repo name (fuzzy-matched). Omit to search every indexed
              repo (results are merged and sorted by date).
        path: Optional file/dir path within the repo to restrict
              history to (e.g. "src/auth/JwtFilter.java").
        grep: Optional regex matched against commit messages
              (git log --grep, case-insensitive).
        author: Optional author name/email substring.
        since: Optional date filter ("2 weeks ago", "2026-05-01").
        limit: Max commits per repo (default 20, capped at 100).
    """
    repo, err = resolve_repo(repo)
    if err:
        return err
    targets = _git_repos(repo)
    if not targets:
        return ("No git repositories found under data/repos/ "
                f"(repo={repo!r})." if repo else
                "No git repositories found under data/repos/.")

    limit = max(1, min(int(limit), 100))
    args = ["log", f"--pretty=format:{_FMT}", "-n", str(limit), "--no-merges"]
    if grep:
        args += ["--grep", grep, "-i", "--extended-regexp"]
    if author:
        args += ["--author", author]
    if since:
        args += ["--since", since]
    if path:
        args += ["--", path]

    all_commits: list[dict] = []
    for name, p in targets:
        out = _run_git(p, args)
        if out.startswith("["):
            all_commits.append({"repo": name, "error": out})
            continue
        all_commits.extend(_parse_log(out, name))

    if not repo:
        all_commits.sort(key=lambda c: c.get("date", ""), reverse=True)
        all_commits = all_commits[:limit]

    if not all_commits:
        what = f"matching {grep!r}" if grep else ""
        where = f" in {repo}" if repo else " across all repos"
        return f"No commits {what}{where}."
    return json.dumps(all_commits, indent=2)


@tool
def find_commits_for_jira(ticket: str) -> str:
    """Find every commit across ALL indexed repos that references a
    JIRA ticket (e.g. "PROJ-1234") in its commit message.

    Use this to answer: "what changed for PROJ-1234", "which repos
    were touched by PROJ-567". Returns JSON grouped by repo.
    """
    ticket = ticket.strip().upper()
    if not _JIRA_RE.fullmatch(ticket):
        return (f"'{ticket}' doesn't look like a JIRA key "
                f"(expected e.g. PROJ-1234).")
    targets = _git_repos(None)
    if not targets:
        return "No git repositories found under data/repos/."

    # Word-boundary so PROJ-1 doesn't match PROJ-10, PROJ-1234, …
    pattern = rf"\b{re.escape(ticket)}\b"
    by_repo: dict[str, list[dict]] = {}
    for name, p in targets:
        out = _run_git(p, [
            "log", f"--pretty=format:{_FMT}",
            "--perl-regexp", "--grep", pattern, "-i", "--all",
        ])
        if out.startswith("["):
            continue
        commits = _parse_log(out, name)
        if commits:
            by_repo[name] = commits

    if not by_repo:
        return f"No commits reference {ticket} in any indexed repo."
    total = sum(len(v) for v in by_repo.values())
    return json.dumps({
        "ticket": ticket,
        "repos": sorted(by_repo.keys()),
        "total_commits": total,
        "commits": by_repo,
    }, indent=2)


@tool
def git_show(repo: str, sha: str, stat_only: bool = True) -> str:
    """Show a specific commit: message, author, and changed files
    (with line counts). Set stat_only=False to include the full diff
    (truncated to ~8KB).

    Use this after git_log/find_commits_for_jira to inspect what a
    specific commit actually changed.

    Args:
        repo: Repo name (fuzzy-matched).
        sha: Commit SHA (short or full).
        stat_only: If True (default), show --stat summary only.
                   If False, include the unified diff.
    """
    repo, err = resolve_repo(repo)
    if err:
        return err
    targets = _git_repos(repo)
    if not targets:
        return f"Repo {repo!r} not found or has no .git."
    name, p = targets[0]

    args = ["show", "--no-color", "--stat" if stat_only else "--patch",
            "--pretty=format:commit %H%nAuthor: %an <%ae>%nDate:   %aI%n%n"
            "    %s%n%n%w(0,4,4)%b", sha]
    out = _run_git(p, args, timeout=20)
    if out.startswith("[git error"):
        return out
    if len(out) > 8000:
        out = out[:8000] + f"\n… [truncated, {len(out)} chars total]"
    return out or f"Commit {sha} not found in {name}."


@tool
def git_blame(repo: str, path: str, line_start: int,
              line_end: int | None = None) -> str:
    """Show who last changed specific lines of a file, and in which
    commit (with the commit subject). Use this to answer "who wrote
    line N of <file>" or "when was this block last touched".

    Args:
        repo: Repo name (fuzzy-matched).
        path: File path within the repo.
        line_start: First line (1-based).
        line_end: Last line (defaults to line_start).
    """
    repo, err = resolve_repo(repo)
    if err:
        return err
    targets = _git_repos(repo)
    if not targets:
        return f"Repo {repo!r} not found or has no .git."
    name, p = targets[0]
    le = line_end or line_start

    out = _run_git(p, [
        "blame", "--line-porcelain", "-L", f"{line_start},{le}", "--", path,
    ])
    if out.startswith("["):
        return out

    entries: list[dict] = []
    cur: dict = {}
    for ln in out.splitlines():
        if ln.startswith("\t"):
            cur["code"] = ln[1:]
            entries.append(cur)
            cur = {}
        elif " " in ln and not ln.startswith(("author", "committer",
                                              "summary", "filename",
                                              "previous", "boundary")):
            parts = ln.split()
            if len(parts) >= 3 and len(parts[0]) == 40:
                cur = {"sha": parts[0][:10],
                       "line": int(parts[2])}
        elif ln.startswith("author "):
            cur["author"] = ln[7:]
        elif ln.startswith("author-time "):
            cur["author_time"] = int(ln[12:])
        elif ln.startswith("summary "):
            cur["summary"] = ln[8:]

    if not entries:
        return f"No blame for {path}:{line_start}-{le} in {name}."
    return json.dumps({"repo": name, "path": path, "lines": entries},
                      indent=2)
