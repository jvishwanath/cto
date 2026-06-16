"""Connection probes used by the setup wizard + `cto doctor`.

Each probe returns `HealthResult(ok, status, detail)` so callers
can render a uniform ✓/✗ checklist. Probes have hard timeouts
(default 5s) so a wedged service can't hang the wizard.

All probes are stdlib-only (httpx + psycopg are used by the rest
of the app but we keep these self-contained so they work from
the binary's thin-client mode without dragging the full ML stack).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    status: str          # short one-line summary, e.g. "200 OK"
    detail: str = ""     # error context, redacted in default output


# ── HTTP probe (no httpx dep — keeps thin-client binary small) ────────

def _http_get(url: str, *,
              headers: dict[str, str] | None = None,
              timeout: float = 5.0) -> HealthResult:
    if not url:
        return HealthResult(False, "no URL")
    req = urllib.request.Request(url, method="GET",
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return HealthResult(True, f"{r.status} {r.reason}")
    except urllib.error.HTTPError as e:
        return HealthResult(False, f"{e.code} {e.reason}",
                            detail=str(e))
    except urllib.error.URLError as e:
        return HealthResult(False, "unreachable", detail=str(e.reason))
    except (socket.timeout, TimeoutError):
        return HealthResult(False, f"timeout after {timeout}s")
    except Exception as e:
        return HealthResult(False, type(e).__name__, detail=str(e))


# ── CTO server / remote-mode probes ──────────────────────────────────

def probe_cto_server(url: str, api_key: str = "",
                     timeout: float = 5.0) -> HealthResult:
    """GET <server>/health — expects 200. If api_key given, also
    GET /sessions to verify auth works."""
    base = url.rstrip("/")
    r = _http_get(f"{base}/health", timeout=timeout)
    if not r.ok:
        return r
    if api_key:
        auth_r = _http_get(f"{base}/sessions",
                           headers={"Authorization": f"Bearer {api_key}"},
                           timeout=timeout)
        if not auth_r.ok:
            return HealthResult(False, f"auth failed: {auth_r.status}",
                                detail=auth_r.detail)
        return HealthResult(True, "ok (authenticated)")
    return HealthResult(True, "ok (unauthenticated probe)")


# ── LLM gateway ──────────────────────────────────────────────────────

def probe_llm_gateway(url: str, api_key: str = "",
                      timeout: float = 8.0) -> HealthResult:
    """GET <gateway>/v1/models — LiteLLM exposes this."""
    if not url:
        return HealthResult(False, "no URL")
    base = url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = _http_get(f"{base}/v1/models", headers=headers, timeout=timeout)
    if not r.ok:
        return r
    # Try to parse model count for a more useful status line.
    try:
        req = urllib.request.Request(f"{base}/v1/models", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        n = len(data.get("data") or [])
        return HealthResult(True, f"ok ({n} models available)")
    except Exception:
        return HealthResult(True, "ok")


# ── Qdrant ───────────────────────────────────────────────────────────

def probe_qdrant(url: str, timeout: float = 5.0) -> HealthResult:
    """GET <qdrant>/healthz."""
    if not url or url == "bundled":
        return HealthResult(False, "bundled (not yet started)")
    return _http_get(url.rstrip("/") + "/healthz", timeout=timeout)


# ── Postgres ─────────────────────────────────────────────────────────

def probe_postgres(url: str, timeout: float = 5.0) -> HealthResult:
    """`psql -c 'SELECT 1'` via the psql binary if available; falls
    back to a TCP open-port check so we don't require psycopg in the
    thin-client binary."""
    if not url or url == "bundled":
        return HealthResult(False, "bundled (not yet started)")
    if shutil.which("psql"):
        try:
            r = subprocess.run(
                ["psql", url, "-c", "SELECT 1", "-tA"],
                capture_output=True, timeout=timeout, text=True)
            if r.returncode == 0 and r.stdout.strip() == "1":
                return HealthResult(True, "ok (psql SELECT 1)")
            return HealthResult(False, f"psql exit {r.returncode}",
                                detail=r.stderr.strip()[:300])
        except subprocess.TimeoutExpired:
            return HealthResult(False, f"psql timeout after {timeout}s")
        except Exception as e:
            return HealthResult(False, type(e).__name__, detail=str(e))
    # Fallback: parse host/port and test TCP connect.
    p = urlparse(url)
    host = p.hostname or "localhost"
    port = p.port or 5432
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return HealthResult(True, f"ok (tcp {host}:{port})")
    except Exception as e:
        return HealthResult(False, f"tcp {host}:{port} closed",
                            detail=str(e))


# ── Docker availability ──────────────────────────────────────────────

def probe_docker(timeout: float = 5.0) -> HealthResult:
    """`docker info` exit 0 — required for local-mode bootstrap."""
    if not shutil.which("docker"):
        return HealthResult(False, "docker not on PATH")
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, timeout=timeout, text=True)
        if r.returncode != 0:
            return HealthResult(False, "docker daemon unreachable",
                                detail=r.stderr.strip()[:300])
        return HealthResult(True, f"ok (docker {r.stdout.strip()})")
    except subprocess.TimeoutExpired:
        return HealthResult(False, f"docker timeout after {timeout}s")
    except Exception as e:
        return HealthResult(False, type(e).__name__, detail=str(e))


# ── JIRA / Confluence ────────────────────────────────────────────────

def probe_jira(base_url: str, token: str,
               timeout: float = 6.0) -> HealthResult:
    if not base_url or not token:
        return HealthResult(False, "url or token missing")
    url = f"{base_url.rstrip('/')}/rest/api/2/myself"
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/json"}
    r = _http_get(url, headers=headers, timeout=timeout)
    if not r.ok:
        return r
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return HealthResult(
            True,
            f"ok ({data.get('displayName') or data.get('name')})")
    except Exception:
        return HealthResult(True, "ok")


def probe_confluence(base_url: str, token: str,
                     timeout: float = 6.0) -> HealthResult:
    if not base_url or not token:
        return HealthResult(False, "url or token missing")
    url = f"{base_url.rstrip('/')}/rest/api/user/current"
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/json"}
    return _http_get(url, headers=headers, timeout=timeout)


# ── Redaction ────────────────────────────────────────────────────────

def redact(s: str) -> str:
    """Conservative redaction for doctor output: hostnames stripped
    to scheme + 'redacted'; tokens longer than 8 chars masked."""
    import re
    if not s:
        return s
    # URLs → keep scheme + 'redacted-host'
    s = re.sub(r"(https?://)[^/\s]+", r"\1<redacted>", s)
    # JWT-ish + long base64
    s = re.sub(r"\b(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b",
               "***JWT***", s)
    s = re.sub(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "***AWS***", s)
    # Long opaque tokens — replace middle bytes
    def _mask(m):
        t = m.group(0)
        return t[:3] + "…" + t[-2:] if len(t) > 12 else "***"
    s = re.sub(r"\b[A-Za-z0-9_-]{24,}\b", _mask, s)
    # Filesystem paths under $HOME → ~/
    home = os.path.expanduser("~")
    if home and home != "/":
        s = s.replace(home, "~")
    return s
