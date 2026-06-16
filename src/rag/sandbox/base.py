"""
Sandbox protocol — backends (Docker, E2B, ...) implement run().
"""

import re
from dataclasses import dataclass
from typing import Protocol


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Sandbox(Protocol):
    def run(self, code: str, lang: str = "python",
            repo: str | None = None, timeout: int = 30) -> SandboxResult:
        ...


# ── Output sanitization helpers ─────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_MAX_OUTPUT_BYTES = 4096


def sanitize_output(s: str | bytes, limit: int = _MAX_OUTPUT_BYTES) -> str:
    """Strip ANSI codes, handle binary, truncate to last `limit` bytes."""
    if isinstance(s, bytes):
        # Binary detection: null bytes or decode failure
        if b"\x00" in s:
            return f"<binary output, {len(s)} bytes>"
        try:
            s = s.decode("utf-8")
        except UnicodeDecodeError:
            return f"<non-utf8 output, {len(s)} bytes>"

    s = _ANSI_RE.sub("", s)

    if len(s) > limit:
        s = f"...<truncated {len(s) - limit} bytes>...\n" + s[-limit:]

    return s
