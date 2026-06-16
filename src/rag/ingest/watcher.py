"""
Filesystem watcher: monitors data/repos/ and data/docs/, debounces
events, and dispatches to the incremental indexer via a worker queue.

Design:
  watchdog Observer (FSEvents on macOS, inotify on Linux)
    ├─ ReposHandler: collects per-repo {changed, deleted} during a
    │   3s quiet window, then enqueues one IndexRepoJob.
    └─ DocsHandler: per-file 1s debounce → IndexDocJob / DeleteDocJob.
  Worker thread: serial consumer (one job at a time → no DB races).
"""

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from ..config import REPOS_DIR
from .incremental import index_repo, delete_repo, list_repos
from .docs import index_doc, delete_doc, list_docs, DOCS_DIR, _DOC_EXTS

log = logging.getLogger(__name__)

REPO_DEBOUNCE_SEC = 3.0
DOC_DEBOUNCE_SEC = 1.0
_IGNORE_NAMES = {".DS_Store", ".gitkeep"}
_IGNORE_DIR_PARTS = {".git", ".idea", ".vscode", "__pycache__",
                     "node_modules", "vendor", "build", "target", "dist",
                     "data", ".claude", ".venv", "venv"}


# ── Jobs ─────────────────────────────────────────────────────────────

@dataclass
class IndexRepoJob:
    repo: str
    changed: set[str] = field(default_factory=set)
    deleted: set[str] = field(default_factory=set)
    is_new_repo: bool = False
    is_removed_repo: bool = False


@dataclass
class IndexDocJob:
    path: Path


@dataclass
class DeleteDocJob:
    rel_path: str


@dataclass
class ReloadSkillsJob:
    paths: set[str] = field(default_factory=set)


# ── Debounce helper ──────────────────────────────────────────────────

class _Debouncer:
    """Per-key timer: reset on each touch, fire callback when key has
    been quiet for `delay` seconds."""

    def __init__(self, delay: float, on_fire):
        self.delay = delay
        self.on_fire = on_fire
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def touch(self, key: str):
        with self._lock:
            t = self._timers.get(key)
            if t:
                t.cancel()
            t = threading.Timer(self.delay, self._fire, args=(key,))
            t.daemon = True
            self._timers[key] = t
            t.start()

    def _fire(self, key: str):
        with self._lock:
            self._timers.pop(key, None)
        self.on_fire(key)

    def cancel_all(self):
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()


# ── Handlers ─────────────────────────────────────────────────────────

def _should_ignore(path: Path) -> bool:
    if path.name in _IGNORE_NAMES or path.name.startswith("."):
        return True
    return any(p in _IGNORE_DIR_PARTS for p in path.parts)


class ReposHandler(FileSystemEventHandler):
    """
    Watches data/repos/. Collects {changed, deleted} relative paths per
    repo across the debounce window, then emits one IndexRepoJob.
    """

    def __init__(self, repos_dir: Path, jobq: "queue.Queue"):
        self.repos_dir = repos_dir
        self.jobq = jobq
        self._pending: dict[str, IndexRepoJob] = {}
        self._known_repos: set[str] = {
            d.name for d in repos_dir.iterdir() if d.is_dir()
        } if repos_dir.exists() else set()
        self._lock = threading.Lock()
        self._debounce = _Debouncer(REPO_DEBOUNCE_SEC, self._flush)

    def _repo_and_rel(self, src: str) -> tuple[str, str] | None:
        # Do NOT .resolve() — it follows symlinks, and data/repos/* are
        # typically symlinks pointing outside the watched tree, which
        # would make relative_to() fail and silently drop the event.
        src_p = Path(src).absolute()
        base_p = self.repos_dir.absolute()
        try:
            rel = src_p.relative_to(base_p)
        except ValueError:
            return None
        parts = rel.parts
        if not parts:
            return None
        return parts[0], str(Path(*parts[1:])) if len(parts) > 1 else ""

    def _record(self, src: str, *, deleted: bool):
        info = self._repo_and_rel(src)
        if not info:
            return
        repo, rel = info
        if rel and _should_ignore(Path(rel)):
            return

        with self._lock:
            job = self._pending.setdefault(repo, IndexRepoJob(repo=repo))
            if rel == "":
                # Event on the repo dir itself (created/deleted)
                if deleted:
                    job.is_removed_repo = True
                else:
                    job.is_new_repo = repo not in self._known_repos
            elif deleted:
                job.deleted.add(rel)
                job.changed.discard(rel)
            else:
                job.changed.add(rel)
                job.deleted.discard(rel)

        self._debounce.touch(repo)

    def _flush(self, repo: str):
        with self._lock:
            job = self._pending.pop(repo, None)
            if job is None:
                return
            if job.is_removed_repo:
                self._known_repos.discard(repo)
            else:
                self._known_repos.add(repo)
        log.info("watcher: enqueue repo=%s changed=%d deleted=%d new=%s removed=%s",
                 repo, len(job.changed), len(job.deleted),
                 job.is_new_repo, job.is_removed_repo)
        self.jobq.put(job)

    # watchdog callbacks
    def on_created(self, event: FileSystemEvent):
        self._record(event.src_path, deleted=False)

    def on_modified(self, event: FileSystemEvent):
        if not event.is_directory:
            self._record(event.src_path, deleted=False)

    def on_deleted(self, event: FileSystemEvent):
        self._record(event.src_path, deleted=True)

    def on_moved(self, event: FileSystemEvent):
        self._record(event.src_path, deleted=True)
        self._record(event.dest_path, deleted=False)


class _SymlinkRepoHandler(FileSystemEventHandler):
    """
    Watches the resolved target of a symlinked repo and forwards events
    to ReposHandler with the correct (link-name, relpath) so debouncing
    and job emission stay unified.
    """

    def __init__(self, repo_name: str, target: Path, jobq: "queue.Queue",
                 parent: "ReposHandler"):
        self.repo = repo_name
        self.target = target
        self.parent = parent

    def _forward(self, src: str, *, deleted: bool):
        try:
            rel = str(Path(src).absolute().relative_to(self.target))
        except ValueError:
            return
        if rel in (".", ""):
            return
        if _should_ignore(Path(rel)):
            return
        with self.parent._lock:
            job = self.parent._pending.setdefault(
                self.repo, IndexRepoJob(repo=self.repo))
            if deleted:
                job.deleted.add(rel)
                job.changed.discard(rel)
            else:
                job.changed.add(rel)
                job.deleted.discard(rel)
        self.parent._debounce.touch(self.repo)

    def on_created(self, e):  self._forward(e.src_path, deleted=False)
    def on_modified(self, e):
        if not e.is_directory: self._forward(e.src_path, deleted=False)
    def on_deleted(self, e):  self._forward(e.src_path, deleted=True)
    def on_moved(self, e):
        self._forward(e.src_path, deleted=True)
        self._forward(e.dest_path, deleted=False)


class DocsHandler(FileSystemEventHandler):
    """Watches data/docs/ — per-file debounce, one job per file."""

    _EXTS = {".pdf", ".md", ".markdown", ".txt"}

    def __init__(self, docs_dir: Path, jobq: "queue.Queue"):
        self.docs_dir = docs_dir
        self.jobq = jobq
        self._pending: dict[str, tuple[str, Path]] = {}  # key → (action, path)
        self._lock = threading.Lock()
        self._debounce = _Debouncer(DOC_DEBOUNCE_SEC, self._flush)

    def _record(self, src: str, action: str):
        p = Path(src)
        if p.suffix.lower() not in self._EXTS or _should_ignore(p):
            return
        key = str(p)
        with self._lock:
            self._pending[key] = (action, p)
        self._debounce.touch(key)

    def _flush(self, key: str):
        with self._lock:
            entry = self._pending.pop(key, None)
        if not entry:
            return
        action, path = entry
        if action == "delete":
            try:
                rel = str(path.absolute().relative_to(self.docs_dir.absolute()))
            except ValueError:
                rel = path.name
            log.info("watcher: enqueue doc DELETE %s", rel)
            self.jobq.put(DeleteDocJob(rel_path=rel))
        else:
            log.info("watcher: enqueue doc INDEX %s", path.name)
            self.jobq.put(IndexDocJob(path=path))

    def on_created(self, e):  self._record(e.src_path, "index")
    def on_modified(self, e):
        if not e.is_directory: self._record(e.src_path, "index")
    def on_deleted(self, e):  self._record(e.src_path, "delete")
    def on_moved(self, e):
        self._record(e.src_path, "delete")
        self._record(e.dest_path, "index")


class SkillsHandler(FileSystemEventHandler):
    """Watches data/skills/*.md. On any .md change: scaffold
    frontmatter if missing, then enqueue ONE ReloadSkillsJob (the
    debounce key is the directory, so a flurry of edits coalesces
    into a single registry refresh)."""

    def __init__(self, skills_dir: Path, jobq: "queue.Queue"):
        self.skills_dir = skills_dir
        self.jobq = jobq
        self._touched: set[str] = set()
        self._lock = threading.Lock()
        self._debounce = _Debouncer(DOC_DEBOUNCE_SEC, self._flush)

    def _record(self, src: str):
        p = Path(src)
        if p.suffix.lower() != ".md" or p.name.startswith(("_", ".")):
            return
        with self._lock:
            self._touched.add(str(p))
        self._debounce.touch("skills")

    def _flush(self, _key: str):
        with self._lock:
            paths, self._touched = self._touched, set()
        log.info("watcher: enqueue skills RELOAD (%d file(s) touched)",
                 len(paths))
        self.jobq.put(ReloadSkillsJob(paths=paths))

    def on_created(self, e):  self._record(e.src_path)
    def on_modified(self, e):
        if not e.is_directory: self._record(e.src_path)
    def on_deleted(self, e):  self._record(e.src_path)
    def on_moved(self, e):
        self._record(e.src_path)
        self._record(e.dest_path)


# ── Worker ───────────────────────────────────────────────────────────

def _worker(jobq: "queue.Queue", stop: threading.Event):
    # Lazy import to avoid circular (tools → ingest → tools).
    try:
        from ..agents.tools._repo import invalidate_repo_cache
    except Exception:
        invalidate_repo_cache = lambda: None
    try:
        from .symbol_mentions import invalidate_symbol_cache
    except Exception:
        invalidate_symbol_cache = lambda: None

    log.info("watcher worker: started")
    while not stop.is_set():
        try:
            job = jobq.get(timeout=0.5)
        except queue.Empty:
            continue
        t0 = time.perf_counter()
        try:
            if isinstance(job, IndexRepoJob):
                if job.is_removed_repo:
                    r = delete_repo(job.repo)
                elif job.is_new_repo or (not job.changed and not job.deleted):
                    r = index_repo(job.repo, files=None)
                else:
                    r = index_repo(job.repo,
                                   files=sorted(job.changed),
                                   deleted=job.deleted)
                dt = (time.perf_counter() - t0) * 1000
                log.info("watcher: %s mode=%s files=%d chunks=%d sym=%d edges=%d "
                         "%s(%.0fms)",
                         r.repo, r.mode, r.files_processed, r.chunks,
                         r.symbols, r.edges,
                         f"[escalated: {r.escalation_reason}] " if r.escalated else "",
                         dt)
                invalidate_repo_cache()
                invalidate_symbol_cache()
            elif isinstance(job, IndexDocJob):
                n = index_doc(job.path)
                log.info("watcher: doc %s → %d chunks (%.0fms)",
                         job.path.name, n, (time.perf_counter() - t0) * 1000)
            elif isinstance(job, DeleteDocJob):
                delete_doc(job.rel_path)
                log.info("watcher: doc %s deleted (%.0fms)",
                         job.rel_path, (time.perf_counter() - t0) * 1000)
            elif isinstance(job, ReloadSkillsJob):
                from ..skills.registry import (
                    load_skills, scaffold_frontmatter,
                )
                scaffolded = []
                for p in sorted(job.paths):
                    pp = Path(p)
                    if pp.exists() and scaffold_frontmatter(pp):
                        scaffolded.append(pp.name)
                sk = load_skills(refresh=True)
                log.info("watcher: skills reloaded → %s%s (%.0fms)",
                         sorted(sk),
                         f" (scaffolded: {scaffolded})"
                         if scaffolded else "",
                         (time.perf_counter() - t0) * 1000)
        except Exception:
            log.exception("watcher: job failed: %r", job)
        finally:
            jobq.task_done()
    log.info("watcher worker: stopped")


# ── Public API ───────────────────────────────────────────────────────

class IngestWatcher:
    """Starts/stops the observer + worker thread. Usable standalone or
    from FastAPI lifespan."""

    def __init__(self, repos_dir: str | Path | None = None,
                 docs_dir: str | Path | None = None,
                 skills_dir: str | Path | None = None):
        from ..skills.registry import SKILLS_DIR
        self.repos_dir = Path(repos_dir or REPOS_DIR)
        self.docs_dir = Path(docs_dir or DOCS_DIR)
        self.skills_dir = Path(skills_dir or SKILLS_DIR)
        self.jobq: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._observer: Observer | None = None
        self._worker_thread: threading.Thread | None = None
        self._handlers: list = []

    def start(self):
        self.repos_dir.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)

        self.skills_dir.mkdir(parents=True, exist_ok=True)

        self._observer = Observer()
        rh = ReposHandler(self.repos_dir, self.jobq)
        dh = DocsHandler(self.docs_dir, self.jobq)
        sh = SkillsHandler(self.skills_dir, self.jobq)
        self._handlers = [rh, dh, sh]
        self._observer.schedule(rh, str(self.repos_dir), recursive=True)
        self._observer.schedule(dh, str(self.docs_dir), recursive=True)
        self._observer.schedule(sh, str(self.skills_dir), recursive=False)

        # FSEvents/inotify don't follow symlinks. For symlinked repos
        # (the common dev setup), also watch the link TARGET so changes
        # inside the real directory fire events. Map them back via a
        # dedicated handler that knows the repo name.
        repos_real = self.repos_dir.resolve()
        for entry in self.repos_dir.iterdir():
            if entry.is_symlink() and entry.is_dir():
                target = entry.resolve()
                # Refuse self-referential links (project linked into
                # its own data/repos/): watching the target recursively
                # would include data/qdrant etc. and the indexer's own
                # writes would re-trigger indexing forever.
                if (target == repos_real
                        or repos_real in target.parents
                        or target in repos_real.parents):
                    log.warning(
                        "watcher: NOT auto-watching %s → %s (target "
                        "contains/is-contained-by data/repos — would "
                        "self-loop). Indexed once; live edits won't "
                        "auto-reindex.",
                        entry.name, target,
                    )
                    continue
                sh = _SymlinkRepoHandler(entry.name, target, self.jobq, rh)
                self._handlers.append(sh)
                self._observer.schedule(sh, str(target), recursive=True)
                log.info("watcher: also watching symlink target %s → %s",
                         entry.name, target)

        self._observer.start()

        # Startup reconciliation: enqueue jobs for repos/docs that exist
        # on disk but aren't in the index yet (e.g., symlinked while the
        # watcher was down, or first-ever start).
        self._reconcile()

        self._worker_thread = threading.Thread(
            target=_worker, args=(self.jobq, self._stop), daemon=True,
        )
        self._worker_thread.start()

        log.info("IngestWatcher: watching repos=%s docs=%s skills=%s",
                 self.repos_dir, self.docs_dir, self.skills_dir)
        return self

    def _reconcile(self):
        try:
            indexed_repos = {r["repo"] for r in list_repos()}
        except Exception:
            log.warning("reconcile: could not read indexed repos; skipping")
            return

        on_disk = {d.name for d in self.repos_dir.iterdir()
                   if d.is_dir() and not d.name.startswith((".", "_"))}

        for name in sorted(on_disk - indexed_repos):
            log.info("reconcile: repo %r on disk but not indexed → enqueue full", name)
            self.jobq.put(IndexRepoJob(repo=name, is_new_repo=True))

        for name in sorted(indexed_repos - on_disk):
            log.info("reconcile: repo %r indexed but missing from disk → enqueue delete", name)
            self.jobq.put(IndexRepoJob(repo=name, is_removed_repo=True))

        # Docs: same idea
        try:
            indexed_docs = {d["filepath"] for d in list_docs()}
        except Exception:
            indexed_docs = set()
        for f in self.docs_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in _DOC_EXTS and not _should_ignore(f):
                rel = str(f.relative_to(self.docs_dir))
                if rel not in indexed_docs:
                    log.info("reconcile: doc %r on disk but not indexed → enqueue", rel)
                    self.jobq.put(IndexDocJob(path=f))

        # Skills: scaffold any frontmatter-less files dropped in
        # while the server was down, then trigger a reload so the
        # registry reflects current disk state.
        from ..skills.registry import has_frontmatter
        needs_reload = False
        for f in self.skills_dir.glob("*.md"):
            if f.name.startswith(("_", ".")):
                continue
            try:
                if not has_frontmatter(f.read_text(encoding="utf-8")):
                    needs_reload = True
                    log.info("reconcile: skill %r missing frontmatter "
                             "→ scaffold queued", f.name)
            except Exception:
                pass
        if needs_reload:
            self.jobq.put(ReloadSkillsJob(
                paths={str(f) for f in self.skills_dir.glob("*.md")}))

    def stop(self):
        for h in self._handlers:
            if hasattr(h, "_debounce"):
                h._debounce.cancel_all()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2)
        self._stop.set()
        if self._worker_thread:
            self._worker_thread.join(timeout=2)
        log.info("IngestWatcher: stopped")
