"""Synchronous bridge to the MCP server over stdio.

The MCP client is async; the desk is synchronous and deliberately so — the Risk
Officer and the loop are easier to reason about and test without an event loop
in the picture. This module owns a background thread running the async session
and exposes one blocking `call_tool`.

Read-only by construction: `call_tool` refuses any tool name that looks
mutating, so even a mis-mapped capability cannot place an order through here.
Writes go through the CLI adapter and nowhere else (spec section 6).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Sequence

from .alpaca_mcp import MCPUnavailable, is_write_tool

DEFAULT_MCP_DIR = Path(os.getenv("ALPACA_MCP_DIR", "/opt/alpaca-mcp-server"))
CONNECT_TIMEOUT = 60.0
CALL_TIMEOUT = 45.0


#: Alpaca's MCP server wraps every payload as
#:   {"_alpaca_mcp_security": {...}, "data": {...the real payload...}}
#: The security block is a provenance notice marking the contents as untrusted
#: tool output. We honour that by treating the payload strictly as data -- it is
#: parsed into typed values and never interpreted as instructions.
SECURITY_ENVELOPE_KEY = "_alpaca_mcp_security"
ENVELOPE_DATA_KEY = "data"


def unwrap_envelope(payload: Any) -> Any:
    """Return the inner payload when the security envelope is present."""
    if (
        isinstance(payload, dict)
        and SECURITY_ENVELOPE_KEY in payload
        and ENVELOPE_DATA_KEY in payload
    ):
        return payload[ENVELOPE_DATA_KEY]
    return payload


def _extract(result: Any) -> Any:
    """Pull usable data out of an MCP tool result.

    Servers return content blocks; Alpaca's return JSON text inside a security
    envelope. Falls back to the raw text when it is not JSON, so a
    human-readable error is never swallowed.
    """
    # Decode first, unwrap once. Unwrapping at each return site let the JSON-text
    # branch drift out of sync with the structuredContent branch, which silently
    # handed callers the envelope instead of the payload.
    payload: Any

    structured = getattr(result, "structuredContent", None)
    if structured:
        payload = structured
    else:
        content = getattr(result, "content", None) or []
        texts = [t for t in (getattr(block, "text", None) for block in content) if t]
        if not texts:
            return None
        joined = "\n".join(texts)
        try:
            payload = json.loads(joined)
        except json.JSONDecodeError:
            return joined  # a human-readable error is never swallowed

    return unwrap_envelope(payload)


class MCPStdioBridge:
    """Owns the MCP session on a background event loop."""

    def __init__(
        self,
        command: Path | str | None = None,
        args: Sequence[str] = ("--transport", "stdio"),
        env: dict | None = None,
        cwd: Path | str | None = None,
    ):
        self.command = str(command or (DEFAULT_MCP_DIR / ".venv" / "bin" / "alpaca-mcp-server"))
        self.args = list(args)
        self.env = dict(env or os.environ)
        self.env.setdefault("ALPACA_PAPER_TRADE", "true")
        self.cwd = str(cwd or DEFAULT_MCP_DIR)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self.tools: tuple[str, ...] = ()

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> "MCPStdioBridge":
        """Launch the server and complete the handshake. Raises on failure."""
        if self.env.get("ALPACA_PAPER_TRADE", "true").lower() in ("0", "false", "no"):
            raise MCPUnavailable("refusing to connect to a live-trading MCP server")
        if not Path(self.command).exists():
            raise MCPUnavailable(f"MCP server command not found at {self.command}")

        self._thread = threading.Thread(target=self._run, name="mcp-stdio", daemon=True)
        self._thread.start()
        if not self._ready.wait(CONNECT_TIMEOUT):
            raise MCPUnavailable(f"MCP server did not become ready within {CONNECT_TIMEOUT:.0f}s")
        if self._error is not None:
            raise MCPUnavailable(f"MCP connection failed: {self._error}")
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except BaseException as exc:  # noqa: BLE001 - reported to the caller thread
            self._error = exc
            self._ready.set()
        finally:
            loop.close()

    async def _serve(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self.command, args=self.args, env=self.env, cwd=self.cwd
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.tools = tuple(t.name for t in listed.tools)
                    self._session = session
                    self._ready.set()
                    while not self._stop.is_set():
                        await asyncio.sleep(0.1)
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
            self._ready.set()
            raise

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._session = None

    def __enter__(self) -> "MCPStdioBridge":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    # --- the read surface ----------------------------------------------------

    def call_tool(self, name: str, params: dict | None = None) -> Any:
        """Invoke one tool and return its decoded payload. Blocking.

        Refuses mutating tool names outright: this bridge is the READ path, and
        an order must never leave the desk through it.
        """
        if is_write_tool(name):
            raise MCPUnavailable(
                f"refusing to call mutating tool {name!r} over the read bridge; "
                "broker writes go through the Alpaca CLI adapter"
            )
        if self._session is None or self._loop is None:
            raise MCPUnavailable("MCP session is not running; call start() first")

        future: Future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, params or {}), self._loop
        )
        try:
            result = future.result(timeout=CALL_TIMEOUT)
        except Exception as exc:
            raise MCPUnavailable(f"MCP call {name!r} failed: {type(exc).__name__}: {exc}") from exc

        if getattr(result, "isError", False):
            raise MCPUnavailable(f"MCP tool {name!r} returned an error: {_extract(result)}")
        return _extract(result)
