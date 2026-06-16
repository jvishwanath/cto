"""`cto compose <cmd>` — Docker-compose driver for the bundled
infra stack (qdrant + postgres + phoenix). Used by both:

    - the setup wizard (auto-bootstrap on local-mode finish)
    - direct CLI subcommands for ongoing ops:
        cto compose up        # start in background
        cto compose down      # stop + remove containers
        cto compose ps        # show service state
        cto compose logs [s]  # stream logs (one service or all)
        cto compose pull      # pull latest images
        cto compose status    # health-poll all services once

Project name pinned to `cto` (no per-user namespacing in v1).
Compose file extracted on first call to $XDG_DATA_HOME/cto/compose/
docker-compose.yaml — sourced from `packaging/compose.cto-infra.yaml`
shipped inside the binary.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from importlib.resources import files
from pathlib import Path

from rich.console import Console

from . import _config as cfg_mod
from . import _health


console = Console()

PROJECT = "cto"          # docker compose -p cto
SVC_QDRANT = "qdrant"
SVC_POSTGRES = "postgres"
SVC_PHOENIX = "phoenix"
ALL_SERVICES = (SVC_QDRANT, SVC_POSTGRES, SVC_PHOENIX)

HEALTH_TIMEOUT_S = 90    # 90s to come up cold; tunable via env
HEALTH_POLL_S = 2


# ── compose-file location ────────────────────────────────────────────

def compose_dir() -> Path:
    return cfg_mod.data_dir() / "compose"


def compose_file_path() -> Path:
    return compose_dir() / "docker-compose.yaml"


def ensure_compose_file() -> Path:
    """Extract the bundled compose file on first use. Idempotent."""
    dst = compose_file_path()
    if dst.exists():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    # importlib.resources reads files packaged with the binary or
    # the source install. Two source candidates: the installed
    # package (preferred) and the repo's packaging/ dir (dev).
    src = _locate_bundled_compose()
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o644)
    return dst


def _locate_bundled_compose() -> Path:
    """Find the shipped compose file. Looks in (a) packaged
    resource (PyInstaller datas or pip-install package_data),
    then (b) repo's packaging/ dir for dev runs."""
    # (a) Package resource — PyInstaller writes datas under _MEIPASS
    if hasattr(sys, "_MEIPASS"):
        cand = Path(sys._MEIPASS) / "compose.cto-infra.yaml"
        if cand.exists():
            return cand
    # (b) Repo dev path
    here = Path(__file__).resolve()
    for up in (here.parents[3], here.parents[2]):  # repo root candidates
        cand = up / "packaging" / "compose.cto-infra.yaml"
        if cand.exists():
            return cand
    raise FileNotFoundError(
        "bundled compose.cto-infra.yaml not found — install path broken")


# ── compose invocation ───────────────────────────────────────────────

def _compose_env() -> dict[str, str]:
    """Env passed to `docker compose` — sets CTO_DATA_DIR so the
    bind-mounts in compose.cto-infra.yaml land under the user's
    data dir, not pwd."""
    env = dict(os.environ)
    env["CTO_DATA_DIR"] = str(cfg_mod.data_dir())
    return env


def _compose_cmd(*args: str) -> list[str]:
    return ["docker", "compose",
            "-f", str(compose_file_path()),
            "-p", PROJECT,
            *args]


def _run(args: list[str], *,
         capture: bool = False, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a docker-compose subcommand. Streams to terminal unless
    `capture=True`, in which case stdout/stderr come back on the
    CompletedProcess for parsing."""
    if not shutil.which("docker"):
        console.print("[red]docker not on PATH.[/] Install Docker Desktop and retry.")
        sys.exit(2)
    try:
        return subprocess.run(
            args, env=_compose_env(), check=False,
            capture_output=capture, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        console.print(f"[red]docker compose timeout after {timeout}s[/]")
        sys.exit(2)


# ── subcommands ──────────────────────────────────────────────────────

def cmd_pull() -> int:
    """Pull all images. Streams progress."""
    ensure_compose_file()
    cfg_mod.ensure_dirs()
    console.print(f"[bold]Pulling images[/] (project=[cyan]{PROJECT}[/])...")
    r = _run(_compose_cmd("pull"))
    return r.returncode


def cmd_up(detach: bool = True) -> int:
    """Start the stack. Creates data dirs first so bind-mounts work.
    With detach=True (the default), waits for healthchecks before
    returning success."""
    ensure_compose_file()
    cfg_mod.ensure_dirs()
    data = cfg_mod.data_dir()
    for sub in ("qdrant", "postgres", "phoenix"):
        (data / sub).mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]Starting stack[/] (project=[cyan]{PROJECT}[/], "
                  f"data=[cyan]{_health.redact(str(data))}[/])")
    args = ["up"]
    if detach:
        args.append("-d")
    args.append("--remove-orphans")
    r = _run(_compose_cmd(*args))
    if r.returncode != 0:
        console.print(f"[red]docker compose up exit {r.returncode}[/]")
        return r.returncode

    if detach:
        return _wait_healthy()
    return 0


def cmd_down(volumes: bool = False) -> int:
    """Stop + remove containers. With volumes=True, also wipes
    bind-mounted data (destructive — separate confirmation in the
    CLI entry point, not here)."""
    if not compose_file_path().exists():
        console.print("[yellow]No compose file extracted — nothing to do.[/]")
        return 0
    args = ["down", "--remove-orphans"]
    if volumes:
        args.append("-v")
    r = _run(_compose_cmd(*args))
    return r.returncode


def cmd_ps() -> int:
    """Show service status."""
    ensure_compose_file()
    r = _run(_compose_cmd("ps"))
    return r.returncode


def cmd_logs(service: str = "", follow: bool = False,
             tail: int = 100) -> int:
    """Tail logs from one service or all. follow=True streams."""
    ensure_compose_file()
    args = ["logs", "--tail", str(tail)]
    if follow:
        args.append("-f")
    if service:
        if service not in ALL_SERVICES:
            console.print(f"[red]unknown service {service!r} "
                          f"(known: {ALL_SERVICES})[/]")
            return 2
        args.append(service)
    r = _run(_compose_cmd(*args), timeout=86_400 if follow else 60)
    return r.returncode


def cmd_status() -> int:
    """One-shot health probe of all 3 services. Doesn't wait."""
    ok = True
    qdrant = _health.probe_qdrant("http://localhost:6333")
    pg = _health.probe_postgres("postgresql://rag:rag@localhost:5432/rag")
    ph = _health._http_get("http://localhost:6006/healthz")
    for label, r in (("qdrant", qdrant), ("postgres", pg),
                     ("phoenix", ph)):
        mark = "[green]✓[/]" if r.ok else "[red]✗[/]"
        console.print(f"  {mark} {label}: {r.status}")
        if not r.ok:
            ok = False
    return 0 if ok else 1


# ── health-poll loop after `up -d` ───────────────────────────────────

def _wait_healthy() -> int:
    """Poll all 3 service healthchecks until ready or timeout. Uses
    `docker compose ps` JSON output so we read compose's own view
    of healthcheck state, not duplicating the probe logic."""
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    console.print(f"[bold]Waiting for services[/] "
                  f"(timeout {HEALTH_TIMEOUT_S}s)...")
    last_state: dict[str, str] = {}
    while time.monotonic() < deadline:
        r = _run(_compose_cmd("ps", "--format", "json"), capture=True,
                 timeout=15)
        if r.returncode != 0:
            time.sleep(HEALTH_POLL_S)
            continue
        state = _parse_ps_json(r.stdout)
        if state != last_state:
            for svc in ALL_SERVICES:
                s = state.get(svc, "missing")
                mark = ("[green]✓[/]" if s == "healthy"
                        else "[yellow]…[/]" if s in ("starting", "running")
                        else "[red]✗[/]")
                console.print(f"  {mark} {svc}: {s}")
            last_state = state
        if all(state.get(svc) == "healthy" for svc in ALL_SERVICES):
            console.print(f"[green]✓ all services healthy.[/]")
            return 0
        # If any service crashed (exited), bail with logs.
        for svc in ALL_SERVICES:
            if state.get(svc) in ("exited", "dead", "missing"):
                console.print(
                    f"[red]✗ {svc} is {state[svc]} — fetching logs:[/]")
                cmd_logs(service=svc, tail=50)
                return 2
        time.sleep(HEALTH_POLL_S)
    console.print(f"[red]Timeout after {HEALTH_TIMEOUT_S}s — last state: "
                  f"{last_state}[/]")
    return 2


def _parse_ps_json(out: str) -> dict[str, str]:
    """Parse `docker compose ps --format json` (one JSON object per
    line OR a JSON array, depending on docker version) into
    {service: state}. State is one of: healthy, starting, unhealthy,
    running, exited, paused, dead. 'running' is reported when a
    container has no healthcheck."""
    import json
    items: list[dict] = []
    out = (out or "").strip()
    if not out:
        return {}
    if out.startswith("["):
        try:
            items = json.loads(out)
        except json.JSONDecodeError:
            return {}
    else:
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    state: dict[str, str] = {}
    for it in items:
        svc = it.get("Service") or it.get("service") or ""
        health = it.get("Health") or ""    # "" if no healthcheck
        st = it.get("State") or it.get("state") or ""
        # Compose reports "running (healthy)" textually in some
        # versions; we want a clean value.
        if health:
            state[svc] = health.lower()
        elif st:
            state[svc] = st.lower()
    return state


# ── argparse dispatch (called from cli.py main) ──────────────────────

def dispatch(argv: list[str]) -> int:
    """Argv (after `cto compose`) → subcommand. Returns exit code.
    Kept hand-rolled (no argparse) to keep the binary's startup
    fast and the surface area tiny."""
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    sub = argv[0]
    rest = argv[1:]
    if sub == "pull":
        return cmd_pull()
    if sub == "up":
        return cmd_up(detach="--no-detach" not in rest)
    if sub == "down":
        return cmd_down(volumes="-v" in rest or "--volumes" in rest)
    if sub == "ps":
        return cmd_ps()
    if sub == "logs":
        service = next((a for a in rest
                        if not a.startswith("-")), "")
        follow = "-f" in rest or "--follow" in rest
        tail = 100
        for a in rest:
            if a.startswith("--tail="):
                try:
                    tail = int(a.split("=", 1)[1])
                except ValueError:
                    pass
        return cmd_logs(service=service, follow=follow, tail=tail)
    if sub == "status":
        return cmd_status()
    print(f"unknown subcommand {sub!r}; try `cto compose --help`",
          file=sys.stderr)
    return 2
