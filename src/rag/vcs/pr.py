"""
create_pr (Phase 8-B): push a worktree branch and open a PR/MR.

PR-as-permission-boundary — CTO proposes; reviewer + CI gate the
merge. Hard refusals here (NOT just prompted): protected-branch
names, --force, merge.

Host detection is by the `origin` remote URL:
  - gitlab (incl. self-hosted via GITLAB_URL) → REST POST merge_requests
  - github                                   → `gh pr create` if available,
                                                else REST POST pulls
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from urllib.parse import quote
from urllib.request import Request, urlopen

from ..config import GIT_TOKEN, GITLAB_URL, GITHUB_API
from .worktree import Worktree, _git, BRANCH_PREFIX

log = logging.getLogger(__name__)

_PROTECTED_RE = re.compile(
    r"^(main|master|develop|trunk|release([/-].*)?|stable([/-].*)?)$",
    re.IGNORECASE)
_COMMIT_TYPE_RE = re.compile(
    r"^(feat|fix|chore|docs|refactor|test|perf|sec)"
    r"(\([\w./-]{1,40}\))?!?:\s.+")

CO_AUTHOR = "Co-Authored-By: CTO <noreply@cto.internal>"


def commit_message(kind: str, area: str, desc: str) -> str:
    """`<type>(<area>): <desc>\n\nCo-Authored-By: CTO <…>` — the
    convention `commit_all` and skill remediation share."""
    area = re.sub(r"[^\w./-]+", "-", area)[:40].strip("-") or "repo"
    return f"{kind}({area}): {desc.strip()}\n\n{CO_AUTHOR}"


def commit_all(wt: Worktree, kind: str, area: str, desc: str) -> str:
    if not wt.has_changes():
        return "(nothing to commit)"
    msg = commit_message(kind, area, desc)
    _git(wt.path, ["add", "-A"])
    _git(wt.path, ["commit", "-m", msg])
    return _git(wt.path, ["rev-parse", "--short", "HEAD"]).strip()


def _parse_remote(url: str) -> tuple[str, str, str]:
    """Returns (host_kind, host, owner/name). host_kind ∈
    {gitlab, github, unknown}."""
    m = re.match(
        r"^(?:git@|ssh://git@|https?://(?:[^@]+@)?)"
        r"(?P<host>[^:/]+)[:/](?P<path>.+?)(?:\.git)?/?$", url or "")
    if not m:
        return "unknown", "", ""
    host, path = m.group("host"), m.group("path")
    if "gitlab" in host or (GITLAB_URL and host in GITLAB_URL):
        return "gitlab", host, path
    if "github" in host:
        return "github", host, path
    return "unknown", host, path


def _default_branch(wt: Worktree) -> str:
    out = _git(wt.src, ["symbolic-ref", "--quiet", "--short",
                        "refs/remotes/origin/HEAD"], check=False).strip()
    return out.removeprefix("origin/") or "main"


def _validate_commits(wt: Worktree, base: str) -> None:
    log_out = _git(wt.path, ["log", "--format=%s",
                             f"origin/{base}..HEAD"], check=False)
    bad = [ln for ln in log_out.splitlines()
           if ln.strip() and not _COMMIT_TYPE_RE.match(ln)]
    if bad:
        raise ValueError(
            f"{len(bad)} commit(s) violate `<type>(<area>): <desc>` "
            f"convention — first: {bad[0]!r}. Use commit_all()/"
            f"commit_message() or rewrite the subject.")


def _http_post(url: str, body: dict, headers: dict) -> dict:
    req = Request(url, data=json.dumps(body).encode("utf-8"),
                  headers={"Content-Type": "application/json", **headers},
                  method="POST")
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def create_pr(wt: Worktree, title: str, body: str = "") -> str:
    """Push `wt.branch` and open a PR/MR against the repo's default
    branch. Returns the PR URL. Raises on refusal/failure."""
    if not wt.branch.startswith(BRANCH_PREFIX):
        raise ValueError(
            f"refusing: branch {wt.branch!r} is not a CTO worktree "
            f"branch (must start with {BRANCH_PREFIX!r})")
    if _PROTECTED_RE.match(wt.branch):
        raise ValueError(f"refusing: {wt.branch!r} is a protected name")
    if not wt.has_commits():
        raise ValueError("refusing: no commits on the worktree branch")

    base = _default_branch(wt)
    if _PROTECTED_RE.match(base) is None:
        # Unusual but allowed — the *target* may be e.g. an env branch.
        pass
    _validate_commits(wt, base)

    # No --force, ever. -u sets upstream so list_orphans() skips it.
    _git(wt.path, ["push", "-u", "origin", wt.branch], timeout=120)

    remote = wt.remote_url()
    kind, host, project = _parse_remote(remote)
    body_full = (body.rstrip() + f"\n\n---\n_{CO_AUTHOR}_\n").strip()

    if kind == "gitlab":
        if not GIT_TOKEN:
            raise RuntimeError("GIT_TOKEN unset — cannot open GitLab MR")
        api = (GITLAB_URL or f"https://{host}").rstrip("/")
        url = (f"{api}/api/v4/projects/{quote(project, safe='')}"
               f"/merge_requests")
        d = _http_post(url, {
            "source_branch": wt.branch, "target_branch": base,
            "title": title, "description": body_full,
            "remove_source_branch": True,
        }, {"PRIVATE-TOKEN": GIT_TOKEN})
        return d.get("web_url") or json.dumps(d)

    if kind == "github":
        if shutil.which("gh"):
            r = subprocess.run(
                ["gh", "pr", "create", "--head", wt.branch,
                 "--base", base, "--title", title, "--body", body_full],
                cwd=str(wt.path), capture_output=True, text=True,
                timeout=60)
            if r.returncode == 0:
                return r.stdout.strip()
            log.warning("[create_pr] gh failed (%s); falling back to "
                        "REST", r.stderr.strip())
        if not GIT_TOKEN:
            raise RuntimeError("GIT_TOKEN unset — cannot open GitHub PR")
        d = _http_post(
            f"{GITHUB_API}/repos/{project}/pulls",
            {"head": wt.branch, "base": base,
             "title": title, "body": body_full},
            {"Authorization": f"Bearer {GIT_TOKEN}",
             "Accept": "application/vnd.github+json"})
        return d.get("html_url") or json.dumps(d)

    raise RuntimeError(
        f"unsupported git host for remote {remote!r} (kind={kind}); "
        f"branch was pushed — open the PR manually.")
