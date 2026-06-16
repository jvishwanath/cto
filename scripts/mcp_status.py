"""Probe each configured MCP server and print tool / capability
counts. No-op when no servers are configured.

Usage:  python scripts/mcp_status.py
        (wired as `make mcp-status`)
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag.mcp.client import (         # noqa: E402
    caps, connect_all, shutdown_all, specs, tools_cache,
)


async def main() -> int:
    await connect_all()
    if not specs():
        print("0 server(s) configured")
        return 0
    print(f"{len(specs())} server(s) configured:")
    for name in sorted(specs()):
        t = tools_cache().get(name, [])
        c = caps().get(name, {})
        flags = ",".join(k for k, v in c.items() if v) or "-"
        print(f"  {name:20s}  tools={len(t):3d}  caps={flags}")
    await shutdown_all()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
