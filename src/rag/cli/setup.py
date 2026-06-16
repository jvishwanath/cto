"""`cto setup` — interactive wizard for first-run config + re-config.

Single-pass: every invocation walks the entire question tree (no
sub-wizards). Defaults are pre-filled from any existing config so
a re-run is mostly hitting Enter.

Three branches at the top:
    remote → connect to a CTO server (collect URL + auth + prefs)
    local  → bootstrap local stack via Docker (collect infra config)
    skip   → write a minimal config and open it in $EDITOR

Built on prompt_toolkit (already a CLI dep) so the binary's
existing input layer handles it. No new dependencies.
"""

from __future__ import annotations

import getpass
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from . import _config as cfg_mod
from . import _health


console = Console()
ACCENT = "bold cyan"
WARN = "bold yellow"
ERR = "bold red"
OK = "green"
DIM = "dim"


def _ask(prompt: str, default: str = "", *,
         choices: list[str] | None = None,
         secret: bool = False,
         allow_empty: bool = True) -> str:
    """Single question. Returns user input or default. Ctrl-C
    propagates so wizard can clean up."""
    hint = f" [{default}]" if default and not secret else ""
    if choices:
        hint = f" [{'/'.join(choices)}]"
        if default:
            hint = f" [{'/'.join(c if c != default else c.upper() for c in choices)}]"
    while True:
        try:
            if secret:
                ans = getpass.getpass(f"  {prompt}: ").strip()
            else:
                ans = input(f"  {prompt}{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise
        if not ans:
            ans = default
        if choices and ans not in choices:
            console.print(f"    [yellow]choose one of {choices}[/]")
            continue
        if not ans and not allow_empty:
            console.print("    [yellow]value required[/]")
            continue
        return ans


def _bool(prompt: str, default: bool = False) -> bool:
    d = "y" if default else "n"
    ans = _ask(prompt, default=d, choices=["y", "n"]).lower()
    return ans == "y"


def _section(title: str) -> None:
    console.print()
    console.print(Rule(title, style=ACCENT))


def _check(label: str, result: _health.HealthResult) -> None:
    mark = "[green]✓[/]" if result.ok else "[red]✗[/]"
    console.print(f"  {mark} {label}: {result.status}")
    if not result.ok and result.detail:
        console.print(f"      [dim]{_health.redact(result.detail)[:200]}[/]")


def _welcome(existing: bool) -> None:
    console.print()
    title = ("Re-running CTO setup" if existing else
             "Welcome to CTO setup")
    console.print(Panel(
        Text.from_markup(
            f"[{ACCENT}]{title}[/]\n\n"
            "This wizard collects connection settings and writes them\n"
            f"to [cyan]{cfg_mod.config_path()}[/] +\n"
            f"[cyan]{cfg_mod.credentials_path()}[/] (mode 600).\n\n"
            "Press [cyan]Ctrl-C[/] at any time to abort — nothing is\n"
            "persisted until the very last step."),
        border_style=ACCENT, padding=(1, 2)))


# ── branches ─────────────────────────────────────────────────────────

def _ask_remote(cfg: cfg_mod.Config,
                creds: dict[str, str]) -> None:
    _section("Remote mode — CTO server connection")
    cfg.remote.url = _ask(
        "Server URL", default=cfg.remote.url, allow_empty=False)
    cfg.remote.auth_method = _ask(
        "Auth method", default=cfg.remote.auth_method or "api-key",
        choices=["api-key", "oidc", "forwarded"])

    if cfg.remote.auth_method == "api-key":
        existing = creds.get("remote.api_key", "")
        new = _ask("API key (leave blank to keep existing)",
                   default="" if not existing else "<existing>",
                   secret=True)
        if new and new != "<existing>":
            creds["remote.api_key"] = new

    cfg.remote.display_name = _ask(
        "Display name (shown in trace tags)",
        default=cfg.remote.display_name or os.environ.get("USER", "anon"))

    # Probe connection
    _check("server health",
           _health.probe_cto_server(cfg.remote.url,
                                    creds.get("remote.api_key", "")))


def _ask_local(cfg: cfg_mod.Config,
               creds: dict[str, str]) -> None:
    _section("Local mode — Docker stack")

    docker = _health.probe_docker()
    _check("docker", docker)
    if not docker.ok:
        console.print(f"  [{ERR}]Local mode requires Docker. "
                      f"Install Docker Desktop and re-run.[/]")
        console.print(f"  [{DIM}]Or pick remote mode to connect to "
                      f"an existing CTO server.[/]")
        raise SystemExit(2)

    default_data = (cfg.local.data_dir or
                    str(cfg_mod.data_dir()))
    cfg.local.data_dir = _ask("Data dir", default=default_data)

    _section("LLM gateway (LiteLLM proxy)")
    cfg.local.llm_gateway_url = _ask(
        "Gateway URL", default=cfg.local.llm_gateway_url,
        allow_empty=False)
    existing_llm = creds.get("local.llm_api_key", "")
    new_llm = _ask("LLM API key (blank to keep existing)",
                   default="" if not existing_llm else "<existing>",
                   secret=True)
    if new_llm and new_llm != "<existing>":
        creds["local.llm_api_key"] = new_llm
    _check("gateway",
           _health.probe_llm_gateway(cfg.local.llm_gateway_url,
                                     creds.get("local.llm_api_key", "")))

    cfg.local.embedding_model = _ask(
        "Embedding model", default=cfg.local.embedding_model)

    _section("Data stores")
    cfg.local.postgres_url = _ask(
        "Postgres (bundled or postgres://...)",
        default=cfg.local.postgres_url)
    if cfg.local.postgres_url != "bundled":
        _check("postgres", _health.probe_postgres(cfg.local.postgres_url))

    cfg.local.qdrant_url = _ask(
        "Qdrant (bundled or http://...)",
        default=cfg.local.qdrant_url)
    if cfg.local.qdrant_url != "bundled":
        _check("qdrant", _health.probe_qdrant(cfg.local.qdrant_url))


def _ask_integrations(cfg: cfg_mod.Config,
                      creds: dict[str, str]) -> None:
    _section("Optional integrations")
    if _bool("JIRA integration?", default=bool(cfg.integrations.jira_url)):
        cfg.integrations.jira_url = _ask(
            "JIRA base URL", default=cfg.integrations.jira_url,
            allow_empty=False)
        existing = creds.get("jira.token", "")
        new = _ask("JIRA token (blank to keep existing)",
                   default="" if not existing else "<existing>",
                   secret=True)
        if new and new != "<existing>":
            creds["jira.token"] = new
        _check("jira", _health.probe_jira(cfg.integrations.jira_url,
                                          creds.get("jira.token", "")))
    else:
        cfg.integrations.jira_url = ""

    if _bool("Confluence integration?",
             default=bool(cfg.integrations.confluence_url)):
        cfg.integrations.confluence_url = _ask(
            "Confluence base URL",
            default=cfg.integrations.confluence_url, allow_empty=False)
        existing = creds.get("confluence.token", "")
        new = _ask("Confluence token (blank to keep existing)",
                   default="" if not existing else "<existing>",
                   secret=True)
        if new and new != "<existing>":
            creds["confluence.token"] = new
        _check("confluence",
               _health.probe_confluence(cfg.integrations.confluence_url,
                                        creds.get("confluence.token", "")))
    else:
        cfg.integrations.confluence_url = ""

    if _bool("GitHub PAT (for create_pr)?",
             default=cfg.integrations.github_enabled):
        existing = creds.get("github.token", "")
        new = _ask("GitHub token (blank to keep existing)",
                   default="" if not existing else "<existing>",
                   secret=True)
        if new and new != "<existing>":
            creds["github.token"] = new
        cfg.integrations.github_enabled = True
    else:
        cfg.integrations.github_enabled = False

    if _bool("Slack webhook for scheduled jobs?",
             default=cfg.integrations.slack_webhook_set):
        existing = creds.get("slack.webhook_url", "")
        new = _ask("Slack webhook URL (blank to keep existing)",
                   default="" if not existing else "<existing>",
                   secret=True)
        if new and new != "<existing>":
            creds["slack.webhook_url"] = new
        cfg.integrations.slack_webhook_set = True
    else:
        cfg.integrations.slack_webhook_set = False


def _ask_prefs(cfg: cfg_mod.Config) -> None:
    _section("Preferences")
    cfg.prefs.verbosity = _ask(
        "Default verbosity", default=cfg.prefs.verbosity,
        choices=["terse", "normal", "detailed"])
    cfg.prefs.default_repo = _ask(
        "Default repo scope (blank = all)",
        default=cfg.prefs.default_repo)
    cfg.prefs.theme = _ask(
        "Theme", default=cfg.prefs.theme,
        choices=["auto", "dark", "light"])
    cfg.prefs.open_traces_in_browser = _bool(
        "Auto-open trace links in browser?",
        default=cfg.prefs.open_traces_in_browser)
    cfg.prefs.telemetry = _bool(
        "Anonymous usage telemetry?",
        default=cfg.prefs.telemetry)


# ── entry ────────────────────────────────────────────────────────────

def run(*, force: bool = False) -> int:
    """Run the wizard. Returns exit code: 0 success, 2 abort/error."""
    cfg_mod.ensure_dirs()
    existing = cfg_mod.is_configured()
    cfg = cfg_mod.load_config()
    creds = cfg_mod.load_credentials()

    _welcome(existing)

    try:
        mode = _ask(
            "Run mode",
            default=cfg.mode or "remote",
            choices=["remote", "local", "skip"])
        cfg.mode = mode

        if mode == "remote":
            _ask_remote(cfg, creds)
        elif mode == "local":
            _ask_local(cfg, creds)
        elif mode == "skip":
            console.print(f"  [{DIM}]Skip selected — writing minimal "
                          f"config; edit later with `$EDITOR "
                          f"{cfg_mod.config_path()}`.[/]")

        if mode in ("remote", "local"):
            _ask_integrations(cfg, creds)
            _ask_prefs(cfg)

    except (KeyboardInterrupt, EOFError):
        console.print()
        console.print(f"  [{WARN}]Aborted. No changes written.[/]")
        return 2

    # Persist (atomic).
    cfg_path = cfg_mod.save_config(cfg)
    cred_path = cfg_mod.save_credentials(creds)

    _section("Done")
    console.print(f"  [{OK}]✓[/] config:      [cyan]{cfg_path}[/]")
    console.print(f"  [{OK}]✓[/] credentials: [cyan]{cred_path}[/] (600)")

    if mode == "local":
        console.print()
        if _bool("Pull images + start stack now (qdrant + postgres "
                 "+ phoenix)?", default=True):
            from . import compose as compose_mod
            rc = compose_mod.cmd_pull()
            if rc != 0:
                console.print(
                    f"  [{ERR}]Image pull failed (exit {rc}). "
                    f"Resolve and retry: [cyan]cto compose up[/].[/]")
                return 0  # config still written; user can fix and re-up
            rc = compose_mod.cmd_up(detach=True)
            if rc != 0:
                console.print(
                    f"  [{ERR}]Stack failed to come up healthy. "
                    f"Inspect: [cyan]cto compose logs[/].[/]")
                return 0
            console.print()
            console.print(f"  [{OK}]✓[/] Local stack running.")
            console.print(f"  Stop later: [cyan]cto compose down[/]")
        else:
            console.print(f"  Start later: [cyan]cto compose up[/]")
    elif mode == "remote":
        console.print()
        console.print(f"  Next: [cyan]cto chat[/]")

    console.print(f"  Verify health any time: [cyan]cto doctor[/]")
    console.print()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
