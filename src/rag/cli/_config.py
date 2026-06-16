"""XDG-compliant config + credentials layer for the binary CLI.

Layout (defaults; override with the XDG_* env vars):
    $XDG_CONFIG_HOME/cto/config.yaml        — non-secret settings (644)
    $XDG_CONFIG_HOME/cto/credentials.toml   — secrets (600)
    $XDG_DATA_HOME/cto/                     — repos/, hf-cache/, reports/
    $XDG_STATE_HOME/cto/                    — session history, scheduler state

Atomic writes: every write goes to a `.tmp` file then `os.replace()`s
into place so a Ctrl-C mid-wizard never leaves a half-written config.
Credentials file mode is forced to 0600 regardless of umask.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml


# ── XDG roots ────────────────────────────────────────────────────────

def _xdg(env: str, fallback: str) -> Path:
    """$XDG_<env> or HOME-relative fallback. Always returns absolute."""
    raw = os.environ.get(env, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / fallback).resolve()


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "cto"


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "cto"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "cto"


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "cto"


def config_path() -> Path:
    return config_dir() / "config.yaml"


def credentials_path() -> Path:
    return config_dir() / "credentials.toml"


# ── data classes ─────────────────────────────────────────────────────

@dataclass
class RemoteConfig:
    url: str = ""
    auth_method: str = "api-key"   # api-key | oidc | forwarded
    display_name: str = ""


@dataclass
class LocalInfraConfig:
    data_dir: str = ""
    llm_gateway_url: str = ""
    embedding_model: str = "text-embedding-3-large"
    postgres_url: str = "bundled"      # bundled | postgres://...
    qdrant_url: str = "bundled"        # bundled | http://...
    phoenix_enabled: bool = True


@dataclass
class IntegrationsConfig:
    jira_url: str = ""
    confluence_url: str = ""
    github_enabled: bool = False
    slack_webhook_set: bool = False    # only the boolean — actual URL is a secret


@dataclass
class PrefsConfig:
    verbosity: str = "normal"          # terse | normal | detailed
    default_repo: str = ""
    theme: str = "auto"                # auto | dark | light
    open_traces_in_browser: bool = False
    telemetry: bool = False


@dataclass
class Config:
    """The whole non-secret config. Secrets live in credentials.toml."""
    mode: str = ""                     # remote | local | skip
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    local: LocalInfraConfig = field(default_factory=LocalInfraConfig)
    integrations: IntegrationsConfig = field(default_factory=IntegrationsConfig)
    prefs: PrefsConfig = field(default_factory=PrefsConfig)
    schema_version: int = 1


# ── read ─────────────────────────────────────────────────────────────

def load_config() -> Config:
    """Read config.yaml; return defaults when missing or malformed."""
    p = config_path()
    if not p.exists():
        return Config()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return Config()
    if not isinstance(raw, dict):
        return Config()

    cfg = Config()
    cfg.mode = str(raw.get("mode") or "")
    cfg.schema_version = int(raw.get("schema_version") or 1)
    for section, klass in (("remote", RemoteConfig),
                           ("local", LocalInfraConfig),
                           ("integrations", IntegrationsConfig),
                           ("prefs", PrefsConfig)):
        sect = raw.get(section) or {}
        if not isinstance(sect, dict):
            continue
        # Keep only known fields so an old/garbage key doesn't crash.
        known = {f for f in klass.__dataclass_fields__}
        setattr(cfg, section, klass(**{k: v for k, v in sect.items()
                                       if k in known}))
    return cfg


def load_credentials() -> dict[str, str]:
    """Read credentials.toml → flat dict (e.g. {'remote.api_key': '...'})."""
    p = credentials_path()
    if not p.exists():
        return {}
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}
    return _flatten(raw)


def _flatten(d: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, prefix=f"{key}."))
        elif v is None:
            continue
        else:
            out[key] = str(v)
    return out


# ── write (atomic + 0600 for credentials) ────────────────────────────

def save_config(cfg: Config) -> Path:
    """Atomic write of config.yaml (mode 644). Returns the path."""
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        {
            "schema_version": cfg.schema_version,
            "mode": cfg.mode,
            "remote": asdict(cfg.remote),
            "local": asdict(cfg.local),
            "integrations": asdict(cfg.integrations),
            "prefs": asdict(cfg.prefs),
        },
        sort_keys=False, default_flow_style=False,
    )
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)
    return p


def save_credentials(creds: dict[str, str]) -> Path:
    """Atomic write of credentials.toml at mode 0600. Returns the path.

    Keys may be dotted ('remote.api_key' → [remote] api_key); we
    expand them into TOML sections automatically.
    """
    p = credentials_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Force 0700 on parent dir so credentials are unreadable to others.
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        pass

    nested: dict = {}
    for dotted, val in sorted(creds.items()):
        if val in (None, ""):
            continue
        parts = dotted.split(".")
        node = nested
        for seg in parts[:-1]:
            node = node.setdefault(seg, {})
        node[parts[-1]] = val

    body_lines: list[str] = []
    for section, kv in nested.items():
        if isinstance(kv, dict):
            body_lines.append(f"[{section}]")
            for k, v in sorted(kv.items()):
                body_lines.append(f'{k} = {_toml_escape(v)}')
            body_lines.append("")
        else:
            body_lines.append(f'{section} = {_toml_escape(kv)}')
    body = "\n".join(body_lines).strip() + "\n"

    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return p


def _toml_escape(v) -> str:
    """Conservative TOML string escaping. Secrets must not break parse."""
    s = str(v)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


# ── helpers ──────────────────────────────────────────────────────────

def is_configured() -> bool:
    """True iff a wizard has been run at least once (config.mode set)."""
    return load_config().mode in ("remote", "local")


def ensure_dirs() -> None:
    """Create config + data + state dirs if missing. Idempotent."""
    for d in (config_dir(), data_dir(), state_dir(), cache_dir()):
        d.mkdir(parents=True, exist_ok=True)
    # Lock down config dir — secrets live there.
    try:
        os.chmod(config_dir(), 0o700)
    except OSError:
        pass
