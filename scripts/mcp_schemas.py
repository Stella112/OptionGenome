#!/usr/bin/env python
"""Print the input schema of the tools the desk depends on.

Parameter names come from the connected server, never from memory — the same
rule the CLI reference follows. Run this whenever the MCP server is upgraded;
a renamed parameter is a silent breakage otherwise.

Usage:
    python scripts/mcp_schemas.py [tool_name ...]
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.broker.mcp_stdio import DEFAULT_MCP_DIR  # noqa: E402
from src.main import _load_dotenv  # noqa: E402

DEFAULT_TOOLS = (
    "get_clock",
    "get_account_info",
    "get_all_positions",
    "get_orders",
    "get_option_chain",
    "get_option_latest_quote",
    "get_option_snapshot",
    "get_stock_bars",
)


async def collect(wanted: set[str]) -> list[str]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(DEFAULT_MCP_DIR / ".venv" / "bin" / "alpaca-mcp-server"),
        args=["--transport", "stdio"],
        env=dict(os.environ),
        cwd=str(DEFAULT_MCP_DIR),
    )
    lines: list[str] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for tool in (await session.list_tools()).tools:
                if wanted and tool.name not in wanted:
                    continue
                # Client versions differ on the attribute name for this.
                schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
                required = schema.get("required") or []
                props = list((schema.get("properties") or {}).keys())
                lines.append(f"{tool.name}")
                lines.append(f"    required : {required}")
                lines.append(f"    accepts  : {props[:14]}")
    return lines


def _causes(exc: BaseException, depth: int = 0):
    """Recurse into ExceptionGroups; a nested group otherwise hides the real error."""
    pad = "  " * (depth + 1)
    subs = getattr(exc, "exceptions", ())
    if not subs:
        yield f"{pad}{type(exc).__name__}: {exc}"
        return
    for sub in subs:
        yield from _causes(sub, depth + 1)


def main() -> int:
    _load_dotenv()
    wanted = set(sys.argv[1:]) or set(DEFAULT_TOOLS)
    try:
        lines = asyncio.run(collect(wanted))
    except BaseException as exc:
        print(f"failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        for line in _causes(exc):
            print(f"  cause: {line.strip()}", file=sys.stderr)
        return 1

    out = Path("docs/mcp-schemas.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(lines)
    out.write_text(body + "\n", encoding="utf-8")
    print(body)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
