#!/usr/bin/env python
"""Discover the connected MCP server's tool list at runtime (spec section 5).

Launches alpacahq/alpaca-mcp-server over stdio, asks it what tools it actually
exposes, maps those to the capabilities the desk needs, and writes
docs/mcp-tools.json.

Historical v1 tool names are never hardcoded: whatever the server reports is
what gets mapped. If a required capability is absent, this exits non-zero so a
deployment halts here rather than at trading time.

Usage:
    ALPACA_API_KEY=... ALPACA_SECRET_KEY=... python scripts/discover_mcp.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.broker.alpaca_mcp import (  # noqa: E402
    REQUIRED_CAPABILITIES,
    discover_capabilities,
    write_tool_manifest,
)

MCP_SERVER_DIR = Path(os.getenv("ALPACA_MCP_DIR", "/opt/alpaca-mcp-server"))
MCP_PYTHON = MCP_SERVER_DIR / ".venv" / "bin" / "python"


async def list_tools() -> list[str]:
    """Connect over stdio and return the server's advertised tool names."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    env.setdefault("ALPACA_PAPER_TRADE", "true")

    params = StdioServerParameters(
        command=str(MCP_PYTHON),
        args=["-m", "alpaca_mcp_server.server"],
        env=env,
        cwd=str(MCP_SERVER_DIR),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            response = await session.list_tools()
            return [tool.name for tool in response.tools]


def main() -> int:
    if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_SECRET_KEY"):
        print("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set", file=sys.stderr)
        return 2
    if not MCP_PYTHON.exists():
        print(f"MCP server interpreter not found at {MCP_PYTHON}", file=sys.stderr)
        return 2

    # A live-trading server must never be the one we discover against.
    if os.getenv("ALPACA_PAPER_TRADE", "true").lower() in ("0", "false", "no"):
        print("refusing to discover against a live-trading MCP server", file=sys.stderr)
        return 2

    try:
        tools = asyncio.run(list_tools())
    except Exception as exc:
        print(f"discovery failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"server advertises {len(tools)} tools")

    toolsets = os.getenv("ALPACA_TOOLSETS")
    filter_ = [t.strip() for t in toolsets.split(",")] if toolsets else None
    capabilities = discover_capabilities(tools, toolsets_filter=filter_)

    for capability in REQUIRED_CAPABILITIES:
        mapped = capabilities.mapping.get(capability)
        print(f"  {'OK  ' if mapped else 'MISS'} {capability:<20} -> {mapped or '(none)'}")

    path = write_tool_manifest(capabilities, Path("docs/mcp-tools.json"))
    print(f"wrote {path}")

    if not capabilities.complete:
        print(f"\nHALT: missing capabilities {list(capabilities.missing)}", file=sys.stderr)
        return 1

    print("\nall required capabilities present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
