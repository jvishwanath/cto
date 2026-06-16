"""`cto doctor` — diagnose connectivity + config + permissions.

Default output is REDACTED so users can paste it into a support
channel without leaking secrets or hostnames. `--unsafe` shows
full URLs / paths (user must type the word).

Exit codes:
    0 — every check passes
    1 — one or more checks failed (still prints full report)
    2 — config file missing or unreadable
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from rich.console import Console
from rich.rule import Rule
from rich.table import Table

from . import _config as cfg_mod
from . import _health


console = Console()


def _row(label: str, result: _health.HealthResult,
         redact: bool = True) -> tuple[str, str, str]:
    mark = "[green]✓[/]" if result.ok else "[red]✗[/]"
    detail = result.detail or ""
    if redact:
        detail = _health.redact(detail)
    return (mark, label, f"{result.status}  [dim]{detail[:120]}[/]")


def _check_path(label: str, p: Path,
                mode: int | None = None) -> tuple[str, str, str]:
    if not p.exists():
        return ("[red]✗[/]", label, "missing")
    if mode is not None:
        actual = stat.S_IMODE(p.stat().st_mode)
        if actual != mode:
            return ("[yellow]⚠[/]", label,
                    f"mode {oct(actual)}, expected {oct(mode)}")
    return ("[green]✓[/]", label, "ok")


def run(unsafe: bool = False) -> int:
    redact = not unsafe
    cfg = cfg_mod.load_config()
    creds = cfg_mod.load_credentials()

    if not cfg_mod.is_configured():
        console.print()
        console.print("[red]No config found.[/] Run `cto setup` first.")
        console.print(f"  Expected at: [dim]{cfg_mod.config_path()}[/]")
        return 2

    console.print()
    console.print(Rule("CTO doctor", style="bold cyan"))
    console.print(f"  mode: [cyan]{cfg.mode}[/]")
    console.print(
        f"  config: [dim]{_redact_path(cfg_mod.config_path(), redact)}[/]")

    rows: list[tuple[str, str, str]] = []

    # File-level checks
    rows.append(_check_path("config.yaml", cfg_mod.config_path(),
                            mode=0o644))
    rows.append(_check_path("credentials.toml",
                            cfg_mod.credentials_path(), mode=0o600))
    rows.append(_check_path("data dir", cfg_mod.data_dir()))
    rows.append(_check_path("state dir", cfg_mod.state_dir()))

    # Connectivity by mode
    if cfg.mode == "remote":
        rows.append(_row("CTO server",
                         _health.probe_cto_server(
                             cfg.remote.url,
                             creds.get("remote.api_key", "")),
                         redact=redact))
    elif cfg.mode == "local":
        rows.append(_row("docker daemon", _health.probe_docker(),
                         redact=redact))
        rows.append(_row("LLM gateway",
                         _health.probe_llm_gateway(
                             cfg.local.llm_gateway_url,
                             creds.get("local.llm_api_key", "")),
                         redact=redact))
        rows.append(_row("postgres",
                         _health.probe_postgres(cfg.local.postgres_url),
                         redact=redact))
        rows.append(_row("qdrant",
                         _health.probe_qdrant(cfg.local.qdrant_url),
                         redact=redact))

    # Integrations
    if cfg.integrations.jira_url:
        rows.append(_row("jira",
                         _health.probe_jira(cfg.integrations.jira_url,
                                            creds.get("jira.token", "")),
                         redact=redact))
    if cfg.integrations.confluence_url:
        rows.append(_row("confluence",
                         _health.probe_confluence(
                             cfg.integrations.confluence_url,
                             creds.get("confluence.token", "")),
                         redact=redact))

    # Render
    tbl = Table(show_header=True, header_style="bold",
                expand=False, box=None)
    tbl.add_column("", width=3)
    tbl.add_column("Check", style="cyan", no_wrap=True)
    tbl.add_column("Status")
    for r in rows:
        tbl.add_row(*r)
    console.print()
    console.print(tbl)
    console.print()

    n_fail = sum(1 for r in rows if "[red]" in r[0])
    n_warn = sum(1 for r in rows if "[yellow]" in r[0])

    if n_fail == 0 and n_warn == 0:
        console.print("[green]All checks passed.[/]")
        return 0
    if n_fail == 0:
        console.print(f"[yellow]{n_warn} warning(s).[/]")
        return 0
    console.print(f"[red]{n_fail} failure(s), {n_warn} warning(s).[/]")
    if redact:
        console.print(
            "  [dim]Output is redacted. Re-run with [cyan]--unsafe[/] "
            "for full URLs/paths (do NOT paste in public channels).[/]")
    return 1


def _redact_path(p: Path, redact: bool) -> str:
    if not redact:
        return str(p)
    home = os.path.expanduser("~")
    s = str(p)
    if home and s.startswith(home):
        return "~" + s[len(home):]
    return s


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run(unsafe="--unsafe" in sys.argv))
