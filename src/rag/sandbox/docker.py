"""
Docker-based sandbox.

Two execution modes (Phase 7-A):

  EPHEMERAL  — `docker run --rm` per call. Stateless: every call is a
               fresh container. Tightest defaults (`--network=none`,
               `--read-only`). Used by `execute_code` (script-file
               via `run()`) and by `mode: ephemeral` skills (raw
               command via `run_shell()`).

  PERSISTENT — one named container per skill *run*. `start()` spawns
               it (`docker run -d … sleep infinity`), each
               `exec(cid, cmd)` runs inside it (`docker exec`), state
               (env, cwd, installed packages, /work files) persists
               across calls, `stop(cid)` tears it down. Rootfs is
               writable (so `pip install` works, container-local) and
               network is opt-in. The host stays isolated: same
               `--cap-drop ALL`, `--security-opt no-new-privileges`,
               resource caps, and `/repo:ro`.

`reap()` is the janitor for abandoned persistent containers (user
never resumed an `interrupt()`, server crashed mid-skill).
"""

import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from ..config import REPOS_DIR
from .base import SandboxResult, sanitize_output

log = logging.getLogger(__name__)

IMAGE = "cto-sandbox:latest"
_FALLBACK_IMAGE = "python:3.12-slim"
SKILL_PREFIX = "cto-skill-"

_LANG_CMD = {
    "python": ["python", "/work/main.py"],
    "bash": ["bash", "/work/main.sh"],
    "sh": ["sh", "/work/main.sh"],
}
_LANG_EXT = {"python": "py", "bash": "sh", "sh": "sh"}


def _docker(args: list[str], *, timeout: float = 30.0,
            input: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True,
                          timeout=timeout, input=input)


class DockerSandbox:

    def __init__(self, image: str | None = None,
                 memory: str = "512m", cpus: str = "1",
                 pids_limit: int = 128, output_limit: int = 4096):
        self.image = image or self._pick_image()
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.output_limit = output_limit

    @staticmethod
    def available() -> bool:
        if shutil.which("docker") is None:
            return False
        try:
            return _docker(["info"], timeout=5).returncode == 0
        except Exception:
            return False

    @staticmethod
    def _pick_image() -> str:
        try:
            if _docker(["image", "inspect", IMAGE],
                       timeout=5).returncode == 0:
                return IMAGE
        except Exception:
            pass
        return _FALLBACK_IMAGE

    # ── Shared flag builders ────────────────────────────────────────

    def _isolation_flags(self, *, network: bool, read_only: bool,
                         memory: str | None, root: bool) -> list[str]:
        """Hardening flags common to both modes. The security-
        essential ones (cap-drop, no-new-privs, resource caps,
        /repo:ro) are NEVER relaxed; only network/read_only/user
        vary per call."""
        flags = [
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--memory", memory or self.memory,
            "--cpus", self.cpus,
            f"--pids-limit={self.pids_limit}",
        ]
        if not network:
            flags.append("--network=none")
        if read_only:
            flags += ["--read-only",
                      "--tmpfs", "/tmp:rw,size=64m,noexec"]
        if not root:
            flags += ["--user", "65534:65534"]  # nobody:nogroup
        return flags

    @staticmethod
    def _repo_mount(repo: str | None) -> list[str]:
        if not repo:
            return []
        # Path-jail: refuse anything that escapes data/repos.
        if ".." in Path(repo).parts or Path(repo).is_absolute():
            return []
        repo_path = Path(REPOS_DIR) / repo
        if not repo_path.exists():
            return []
        return ["-v", f"{repo_path.resolve()}:/repo:ro"]

    def _result(self, proc, t0: float, timed_out: bool,
                limit: int | None = None) -> SandboxResult:
        lim = limit or self.output_limit
        return SandboxResult(
            exit_code=124 if timed_out else proc.returncode,
            stdout=sanitize_output(
                proc.stdout if proc else b"", lim),
            stderr=sanitize_output(
                (proc.stderr if proc else b"")
                + (b"\n[sandbox] timeout exceeded" if timed_out else b""),
                min(lim, 8192)),
            timed_out=timed_out,
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )

    # ── EPHEMERAL: script-file (existing execute_code path) ─────────

    def run(self, code: str, lang: str = "python",
            repo: str | None = None, timeout: int = 30) -> SandboxResult:
        """Write `code` to a temp file, mount at /work:ro, run the
        interpreter on it in a fresh hardened container. Unchanged
        contract — used by execute_code / code_executor subgraph."""
        if lang not in _LANG_CMD:
            return SandboxResult(
                exit_code=2, stdout="",
                stderr=f"unsupported language: {lang!r} "
                       f"(supported: {list(_LANG_CMD)})",
                timed_out=False, duration_ms=0)

        with tempfile.TemporaryDirectory(prefix="ragsbx-") as workdir:
            (Path(workdir) / f"main.{_LANG_EXT[lang]}").write_text(
                code, encoding="utf-8")
            cmd = [
                "docker", "run", "--rm",
                *self._isolation_flags(network=False, read_only=True,
                                       memory=None, root=False),
                "-v", f"{workdir}:/work:ro",
                *self._repo_mount(repo),
                self.image, "timeout", f"{timeout}s", *_LANG_CMD[lang],
            ]
            t0 = time.perf_counter()
            try:
                proc = subprocess.run(cmd, capture_output=True,
                                      timeout=timeout + 5)
                return self._result(proc, t0, False)
            except subprocess.TimeoutExpired as e:
                e.returncode = 124
                return self._result(e, t0, True)
            except FileNotFoundError:
                return SandboxResult(127, "", "docker binary not found",
                                     False, 0)

    # ── EPHEMERAL: raw shell command (skills, mode=ephemeral) ───────

    def run_shell(self, cmd: str, *, repo: str | None = None,
                  image: str | None = None, timeout: int = 30,
                  network: bool = False, read_only: bool = True,
                  memory: str | None = None,
                  output_limit: int = 50_000) -> SandboxResult:
        """Run one shell command in a fresh container. The command
        is passed on stdin (`sh -s`) so it never appears in `ps` or
        the docker-events log, and shell-metacharacters in `cmd`
        can't break the argv. /work is a per-call tmpfs (rw)."""
        argv = [
            "docker", "run", "--rm", "-i",
            *self._isolation_flags(network=network, read_only=read_only,
                                   memory=memory, root=False),
            "--tmpfs", "/work:rw,size=256m",
            "-w", "/work",
            *self._repo_mount(repo),
            image or self.image,
            "timeout", f"{timeout}s", "sh", "-s",
        ]
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(argv, capture_output=True,
                                  timeout=timeout + 10,
                                  input=cmd.encode("utf-8"))
            return self._result(proc, t0, False, output_limit)
        except subprocess.TimeoutExpired as e:
            e.returncode = 124
            return self._result(e, t0, True, output_limit)
        except FileNotFoundError:
            return SandboxResult(127, "", "docker binary not found",
                                 False, 0)

    # ── PERSISTENT: one named container per skill run ──────────────

    def start(self, *, repo: str | None = None,
              image: str | None = None, network: bool = False,
              memory: str = "2g", run_timeout: int = 1800,
              name: str | None = None,
              worktree: str | Path | None = None,
              env_passthrough: tuple[str, ...] = (),
              mounts: tuple[str, ...] = ()) -> str:
        """Spawn a long-lived container and return its name (cid).
        Rootfs is WRITABLE (so package installs work, container-
        local); /repo stays :ro; /work is a tmpfs that survives
        across exec() calls — UNLESS `worktree` is given (Phase
        8-D), in which case that host path is bind-mounted at
        /work:rw so the skill's edits land on a real git branch.
        Runs as root inside (needed for apt/pip-global installs) —
        caps still dropped, host isolated. `run_timeout` is
        enforced via the container's PID-1 (`sleep N`) so an
        abandoned container self-terminates; `reap()` is the belt
        to that suspenders."""
        cid = name or f"{SKILL_PREFIX}{uuid.uuid4().hex[:10]}"
        if worktree:
            wt_path = Path(worktree).resolve()
            if not (wt_path / ".git").exists():
                raise RuntimeError(
                    f"worktree mount {wt_path} is not a git checkout")
            work_mount = ["-v", f"{wt_path}:/work:rw"]
        else:
            work_mount = ["--tmpfs", "/work:rw,size=1g"]
        # Phase 9-B: scoped creds for ops skills. Only the
        # allowlisted vars are forwarded — not the host env.
        # Mounts are forced :ro and must already exist.
        env_flags: list[str] = []
        for k in env_passthrough:
            if k in os.environ:
                env_flags += ["-e", f"{k}={os.environ[k]}"]
        extra_mounts: list[str] = []
        for m in mounts:
            parts = m.split(":")
            if len(parts) < 2:
                log.warning("[sandbox] mount %r skipped (need "
                            "src:dst)", m)
                continue
            sp = Path(os.path.expanduser(parts[0])).resolve()
            if not sp.exists():
                log.warning("[sandbox] mount %r skipped (src "
                            "missing)", m)
                continue
            extra_mounts += ["-v", f"{sp}:{parts[1]}:ro"]
        argv = [
            "docker", "run", "-d", "--name", cid,
            "--label", "cto.skill=1",
            *self._isolation_flags(network=network, read_only=False,
                                   memory=memory, root=True),
            *work_mount,
            *env_flags,
            *extra_mounts,
            "-w", "/work",
            *self._repo_mount(repo),
            image or self.image,
            "sleep", str(int(run_timeout)),
        ]
        try:
            r = _docker(argv[1:], timeout=60)
        except FileNotFoundError:
            raise RuntimeError("docker binary not found on host")
        if r.returncode != 0:
            raise RuntimeError(
                f"sandbox start failed: "
                f"{r.stderr.decode('utf-8', 'replace').strip()}")
        log.info("[sandbox] started %s (image=%s repo=%s net=%s mem=%s "
                 "run_timeout=%ds)", cid, image or self.image, repo,
                 network, memory, run_timeout)
        return cid

    def exec(self, cid: str, cmd: str, *, timeout: int = 600,
             output_limit: int = 50_000) -> SandboxResult:
        """Run one shell command inside the persistent container.
        `sh -lc` so login-profile PATH (npm/go/cargo bins) is loaded;
        command on stdin for the same argv-safety reason as
        run_shell()."""
        argv = ["exec", "-i", cid,
                "timeout", f"{timeout}s", "sh", "-lc",
                "exec sh -s"]
        t0 = time.perf_counter()
        try:
            proc = _docker(argv, timeout=timeout + 10,
                           input=cmd.encode("utf-8"))
            # `docker exec` returns 125/126/127 for its own failures
            # (no such container / not running) — surface verbatim.
            return self._result(proc, t0, False, output_limit)
        except subprocess.TimeoutExpired as e:
            e.returncode = 124
            return self._result(e, t0, True, output_limit)

    @staticmethod
    def alive(cid: str) -> bool:
        try:
            r = _docker(["inspect", "-f", "{{.State.Running}}", cid],
                        timeout=5)
            return r.returncode == 0 and r.stdout.strip() == b"true"
        except Exception:
            return False

    @staticmethod
    def stop(cid: str) -> None:
        try:
            _docker(["rm", "-f", cid], timeout=30)
            log.info("[sandbox] removed %s", cid)
        except Exception as e:
            log.warning("[sandbox] stop %s failed: %s", cid, e)

    @staticmethod
    def reap(prefix: str = SKILL_PREFIX,
             older_than_sec: int = 0) -> list[str]:
        """Remove cto-skill-* containers older than the threshold
        (0 = all). Returns the list of removed names. Wired into
        FastAPI lifespan so a server restart cleans up orphans from
        a prior crash; also callable as `make sandbox-reap`."""
        try:
            r = _docker(["ps", "-a",
                         "--filter", f"name={prefix}",
                         "--filter", "label=cto.skill=1",
                         "--format", "{{.Names}}\t{{.CreatedAt}}"],
                        timeout=10)
        except Exception:
            return []
        removed: list[str] = []
        now = time.time()
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            if "\t" not in line:
                continue
            name, created = line.split("\t", 1)
            if older_than_sec > 0:
                # CreatedAt: "2026-06-04 10:23:45 +0000 UTC" — parse
                # leniently; if it fails, reap anyway (orphan).
                try:
                    ts = time.mktime(time.strptime(
                        created.split(" +")[0].split(" -")[0],
                        "%Y-%m-%d %H:%M:%S"))
                    if now - ts < older_than_sec:
                        continue
                except Exception:
                    pass
            DockerSandbox.stop(name)
            removed.append(name)
        if removed:
            log.info("[sandbox] reaped %d orphan(s): %s",
                     len(removed), removed)
        return removed
