"""
Worktree primitive (Phase 8-A).

A *worktree* is a second checkout of an indexed repo, on its own
throwaway branch, sharing the same .git object store. It's the
sandbox for code-mutation: every write CTO performs lands in a
worktree, never on the indexed checkout under data/repos/<repo>
(which the watcher/indexer reads HEAD of). The branch IS the
artifact — it either becomes a PR (8-B `create_pr`) or is
discarded.

  create(repo, prefix)  → Worktree(path, branch, repo)
  discard(wt)           → remove dir + delete branch (no PR opened)
  list_orphans()        → cto/* worktrees with no upstream
  reap()                → discard all orphans (server-restart hook)

Worktree dirs live under data/superdev/<session>/<repo> so they're
(a) outside data/repos/ and invisible to the indexer, and
(b) host-side paths that 8-D can bind-mount into a container.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import REPOS_DIR, WORKTREES_DIR

log = logging.getLogger(__name__)

BRANCH_PREFIX = "cto/"
_REPO_RE = re.compile(r"^[\w.\-]{1,80}$")


def _git(repo_path: Path, args: list[str], *, check: bool = True,
         timeout: int = 30) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if check and r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo_path}: "
            f"{r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


@dataclass(frozen=True)
class Worktree:
    repo: str
    path: Path
    branch: str
    src: Path

    def has_changes(self) -> bool:
        out = _git(self.path, ["status", "--porcelain"], check=False)
        return bool(out.strip())

    def has_commits(self) -> bool:
        # If the upstream isn't set, every commit on the branch is
        # local; compare against the source repo's HEAD instead.
        base = _git(self.src, ["rev-parse", "HEAD"], check=False).strip()
        head = _git(self.path, ["rev-parse", "HEAD"], check=False).strip()
        return bool(base and head and base != head)

    def remote_url(self) -> str:
        return _git(self.src, ["remote", "get-url", "origin"],
                    check=False).strip()


def _src_path(repo: str) -> Path:
    if not _REPO_RE.fullmatch(repo or ""):
        raise ValueError(f"invalid repo name {repo!r}")
    p = Path(REPOS_DIR) / repo
    if not (p / ".git").exists() and not (p.is_dir()
            and any(p.glob("HEAD"))):
        raise ValueError(
            f"{repo!r} is not a git checkout under {REPOS_DIR}")
    return p


def create(repo: str, prefix: str = "superdev",
           session: str | None = None) -> Worktree:
    """Create a worktree of `repo` on a fresh `cto/<prefix>-<ts>`
    branch from the source repo's current HEAD. <1s; shares .git
    objects with the source — no full clone."""
    src = _src_path(repo)
    ts = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", prefix.lower()).strip("-") or "wt"
    branch = f"{BRANCH_PREFIX}{slug}-{ts}"

    base = Path(WORKTREES_DIR) / (session or f"{slug}-{ts}")
    base.mkdir(parents=True, exist_ok=True)
    wt_path = (base / repo).resolve()
    if wt_path.exists():
        existing = attach(repo, wt_path)
        if existing:
            return existing
        raise RuntimeError(f"worktree path already exists: {wt_path}")

    _git(src, ["worktree", "add", "--quiet",
               "-b", branch, str(wt_path), "HEAD"])
    log.info("[worktree] created %s @ %s (branch=%s)",
             repo, wt_path, branch)
    return Worktree(repo=repo, path=wt_path, branch=branch, src=src)


def attach(repo: str, path: str | Path) -> Worktree | None:
    """Re-attach to an existing CTO worktree at `path` (used on
    interrupt() resume / node replay). Returns None if `path`
    isn't a cto/* worktree of `repo`."""
    src = _src_path(repo)
    p = Path(path).resolve()
    if not (p / ".git").exists():
        return None
    branch = _git(p, ["rev-parse", "--abbrev-ref", "HEAD"],
                  check=False).strip()
    if not branch.startswith(BRANCH_PREFIX):
        return None
    return Worktree(repo=repo, path=p, branch=branch, src=src)


def fork(parent: Worktree, idx: int) -> Worktree:
    """Create a sub-worktree branched from `parent.branch`'s
    current tip (Phase 8.5-D parallel subagents). The sub-
    branch is `<parent.branch>-sub-<idx>`; the dir lives at
    `<parent.path>.sub/<idx>` so reap() still finds it under
    the cto/* prefix. Refuses if the parent worktree has
    uncommitted changes (forks would miss them)."""
    if parent.has_changes():
        raise RuntimeError(
            f"refusing fork: parent worktree {parent.path} has "
            f"uncommitted changes — commit first")
    branch = f"{parent.branch}-sub-{idx}"
    sub_dir = parent.path.parent / f"{parent.path.name}.sub"
    sub_dir.mkdir(parents=True, exist_ok=True)
    path = (sub_dir / str(idx)).resolve()
    if path.exists():
        raise RuntimeError(f"sub-worktree path exists: {path}")
    _git(parent.src, ["worktree", "add", "--quiet",
                      "-b", branch, str(path), parent.branch])
    log.info("[worktree] forked %s → %s (branch=%s)",
             parent.branch, path, branch)
    return Worktree(repo=parent.repo, path=path,
                    branch=branch, src=parent.src)


def merge_into(child: Worktree, parent: Worktree
               ) -> tuple[bool, list[str]]:
    """Cherry-pick every commit on `child.branch` (since it
    forked from `parent.branch`) into the parent worktree.
    Returns (clean, picked_shas). On the first conflict,
    aborts that cherry-pick and returns (False, picked_so_far)
    — caller decides whether to keep the child for manual
    resolution. Parent must be clean (refused otherwise)."""
    if parent.has_changes():
        raise RuntimeError(
            "refusing merge: parent worktree is dirty")
    shas = _git(child.path,
                ["rev-list", "--reverse",
                 f"{parent.branch}..HEAD"],
                check=False).split()
    picked: list[str] = []
    for sha in shas:
        r = subprocess.run(
            ["git", "-C", str(parent.path),
             "cherry-pick", "--allow-empty", sha],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            _git(parent.path, ["cherry-pick", "--abort"],
                 check=False)
            log.info("[worktree] merge %s→%s: conflict at %s "
                     "(%d/%d picked)", child.branch,
                     parent.branch, sha[:8], len(picked),
                     len(shas))
            return False, picked
        picked.append(sha)
    log.info("[worktree] merge %s→%s: %d commit(s) clean",
             child.branch, parent.branch, len(picked))
    return True, picked


def discard(wt: Worktree, *, keep_branch: bool = False) -> None:
    """Remove the worktree dir and (unless `keep_branch`) delete the
    branch. Use when no PR was opened. Idempotent."""
    if wt.path.exists():
        _git(wt.src, ["worktree", "remove", "--force", str(wt.path)],
             check=False)
    _git(wt.src, ["worktree", "prune"], check=False)
    if not keep_branch:
        _git(wt.src, ["branch", "-D", wt.branch], check=False)
    try:
        if wt.path.parent.exists() and not any(wt.path.parent.iterdir()):
            wt.path.parent.rmdir()
    except OSError:
        pass
    log.info("[worktree] discarded %s (branch=%s, keep_branch=%s)",
             wt.path, wt.branch, keep_branch)


def list_orphans() -> list[Worktree]:
    """All cto/* worktrees across every indexed repo whose branch
    has no upstream (i.e. never pushed → no PR). These are safe to
    reap on restart."""
    out: list[Worktree] = []
    repos_dir = Path(REPOS_DIR)
    if not repos_dir.exists():
        return out
    for d in sorted(repos_dir.iterdir()):
        if not (d.is_dir() and (d / ".git").exists()):
            continue
        raw = _git(d, ["worktree", "list", "--porcelain"], check=False)
        for block in raw.strip().split("\n\n"):
            lines = dict(
                ln.split(" ", 1) for ln in block.splitlines()
                if " " in ln)
            path = lines.get("worktree")
            branch = (lines.get("branch") or "").removeprefix(
                "refs/heads/")
            if not path or Path(path).resolve() == d.resolve():
                continue
            if not branch.startswith(BRANCH_PREFIX):
                continue
            up = _git(d, ["rev-parse", "--abbrev-ref",
                          f"{branch}@{{upstream}}"], check=False)
            if up.strip():
                continue
            out.append(Worktree(repo=d.name, path=Path(path),
                                branch=branch, src=d))
    return out


def reap() -> list[str]:
    """Discard every orphan worktree. Wired into the FastAPI
    lifespan + `make worktree-reap` so a server restart cleans up
    abandoned superdev sessions / crashed skill remediations."""
    removed: list[str] = []
    for wt in list_orphans():
        try:
            discard(wt)
            removed.append(f"{wt.repo}:{wt.branch}")
        except Exception as e:
            log.warning("[worktree] reap %s failed: %s", wt.path, e)
    if removed:
        log.info("[worktree] reaped %d orphan(s): %s",
                 len(removed), removed)
    return removed
