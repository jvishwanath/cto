"""Infra preflight: verify Qdrant + Postgres (and Phoenix when enabled)
are reachable BEFORE the app boots so users get an actionable hint
instead of a deep-stack httpx/psycopg traceback.

Run from the Makefile (`chat`, `serve` depend on this). Exits 0 when
everything required is up; 1 otherwise.

Uses only stdlib: parses QDRANT_URL / POSTGRES_DSN / PHOENIX_HOST and
opens a TCP socket with a short timeout. Doesn't import the project's
heavyweight clients — keeps preflight startup near-instant."""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class Target:
    name: str
    host: str
    port: int
    hint: str
    required: bool = True


def _parse_dsn(dsn: str) -> tuple[str, int]:
    # postgresql://user:pass@host:port/db  → (host, port)
    u = urlparse(dsn)
    return (u.hostname or "localhost", u.port or 5432)


def _parse_url(url: str, default_port: int) -> tuple[str, int]:
    u = urlparse(url)
    return (u.hostname or "localhost", u.port or default_port)


def _alive(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _targets() -> list[Target]:
    qh, qp = _parse_url(
        os.environ.get("QDRANT_URL", "http://localhost:6333"), 6333)
    ph, pp = _parse_dsn(
        os.environ.get(
            "POSTGRES_DSN",
            "postgresql://rag:rag@localhost:5432/rag"))
    out = [
        Target("Qdrant", qh, qp, "make infra"),
        Target("Postgres", ph, pp, "make infra"),
    ]
    if os.environ.get("PHOENIX_ENABLED", "true").lower() == "true":
        xh, xp = _parse_url(
            os.environ.get("PHOENIX_HOST", "http://localhost:6006"), 6006)
        out.append(Target(
            "Phoenix", xh, xp,
            "make infra  (or set PHOENIX_ENABLED=false to skip)",
            required=False))
    return out


def main() -> int:
    tgts = _targets()
    down: list[Target] = []
    print("Preflight: checking infra…", file=sys.stderr)
    for t in tgts:
        ok = _alive(t.host, t.port)
        mark = "\033[92m✓\033[0m" if ok else \
               ("\033[91m✗\033[0m" if t.required
                else "\033[93m·\033[0m")
        tag = "" if t.required else " (optional)"
        print(f"  {mark} {t.name:9s} {t.host}:{t.port}{tag}",
              file=sys.stderr)
        if not ok:
            down.append(t)

    hard = [t for t in down if t.required]
    soft = [t for t in down if not t.required]
    if soft and not hard:
        print("", file=sys.stderr)
        for t in soft:
            print(f"  note: {t.name} optional — {t.hint}",
                  file=sys.stderr)
    if hard:
        print("", file=sys.stderr)
        print("\033[91mInfra not reachable.\033[0m Start with:",
              file=sys.stderr)
        hints = sorted({t.hint for t in hard})
        for h in hints:
            print(f"    {h}", file=sys.stderr)
        print("", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
