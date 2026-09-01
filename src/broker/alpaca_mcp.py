"""MCP read adapter (spec sections 5 and 6).

ALL broker, account and market state comes through here. This module holds the
only MCP-facing code in the system; application code calls these methods and
never a tool name.

Two rules shape the design:

  * tool names are DISCOVERED at runtime, never hardcoded from a historical
    version of the server. The discovered list is persisted to docs/mcp-tools.json
  * a missing required capability HALTS at startup, not at trading time

Until an MCP server is actually connected, capability verification fails and the
desk halts. That is the intended behaviour, not a gap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

MCP_TOOLS_DOC = Path("docs/mcp-tools.json")


class MCPUnavailable(RuntimeError):
    """Raised when the MCP server is absent, or lacks a required capability.

    Always fatal at startup. There is no degraded mode that trades anyway.
    """


#: Internal capabilities the desk requires, and the candidate tool-name
#: fragments that have historically carried them. Matching is by fragment
#: against the DISCOVERED list -- this is a search hint, never an assumption
#: that any particular name exists.
REQUIRED_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "get_clock": ("clock", "market_clock", "market_status"),
    "get_account": ("account",),
    "get_positions": ("position",),
    "get_open_orders": ("order",),
    "get_option_chain": ("option_chain", "optionchain", "chain", "option_snapshot"),
    "get_quote": ("quote", "latest_quote", "snapshot"),
    "get_bars": ("bar", "historical_bars", "stock_bars"),
}


@dataclass(frozen=True)
class MCPTool:
    name: str
    description: str = ""

    def matches(self, fragment: str) -> bool:
        return fragment.lower() in self.name.lower()


@dataclass
class CapabilityMap:
    """Which discovered tool serves each internal capability."""

    mapping: dict[str, str] = field(default_factory=dict)
    discovered: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing

    def tool_for(self, capability: str) -> str:
        if capability not in self.mapping:
            raise MCPUnavailable(
                f"no MCP tool is mapped to {capability!r}; discovered tools: {list(self.discovered)}"
            )
        return self.mapping[capability]

    def as_dict(self) -> dict:
        return {
            "discovered": list(self.discovered),
            "mapping": dict(self.mapping),
            "missing": list(self.missing),
        }


def discover_capabilities(
    tools: Iterable[MCPTool | str],
    required: Mapping[str, Sequence[str]] | None = None,
    toolsets_filter: Sequence[str] | None = None,
) -> CapabilityMap:
    """Map internal capabilities onto whatever tools the server actually exposes.

    `toolsets_filter` honours ALPACA_TOOLSETS: when set, only tools whose names
    contain one of those toolset fragments are considered.
    """
    required = required or REQUIRED_CAPABILITIES
    normalised = [MCPTool(t) if isinstance(t, str) else t for t in tools]

    if toolsets_filter:
        normalised = [
            t for t in normalised if any(ts.lower() in t.name.lower() for ts in toolsets_filter)
        ]

    mapping: dict[str, str] = {}
    missing: list[str] = []
    for capability, fragments in required.items():
        match = next(
            (t.name for fragment in fragments for t in normalised if t.matches(fragment)),
            None,
        )
        if match is None:
            missing.append(capability)
        else:
            mapping[capability] = match

    return CapabilityMap(
        mapping=mapping,
        discovered=tuple(t.name for t in normalised),
        missing=tuple(missing),
    )


def write_tool_manifest(capabilities: CapabilityMap, path: Path | str = MCP_TOOLS_DOC) -> Path:
    """Persist the discovered tool list (spec section 5)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(capabilities.as_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def load_tool_manifest(path: Path | str = MCP_TOOLS_DOC) -> CapabilityMap | None:
    """Read a previously discovered manifest. None when it is still a placeholder."""
    path = Path(path)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("discovered"):
        return None
    return CapabilityMap(
        mapping=dict(payload.get("mapping", {})),
        discovered=tuple(payload.get("discovered", ())),
        missing=tuple(payload.get("missing", ())),
    )


class AlpacaMCP:
    """Thin read-only adapter over the connected MCP server.

    `call_tool` is injected: it is whatever invokes a tool by name on the live
    MCP connection. Nothing here constructs a tool name of its own, and no
    method on this class writes to the broker.
    """

    def __init__(
        self,
        call_tool: Callable[[str, dict], Any] | None = None,
        capabilities: CapabilityMap | None = None,
    ):
        self._call_tool = call_tool
        self._capabilities = capabilities

    @property
    def capabilities(self) -> CapabilityMap:
        if self._capabilities is None:
            raise MCPUnavailable(
                "MCP capabilities have not been discovered. Connect alpacahq/alpaca-mcp-server "
                "and run discovery before starting the trading loop."
            )
        return self._capabilities

    def verify(self) -> None:
        """Startup gate item 11. Raises MCPUnavailable rather than degrading."""
        if self._call_tool is None:
            raise MCPUnavailable("no MCP connection is configured")
        capabilities = self.capabilities
        if not capabilities.complete:
            raise MCPUnavailable(
                f"connected MCP server is missing required capabilities: {list(capabilities.missing)}. "
                f"Discovered tools: {list(capabilities.discovered)}"
            )

    def _invoke(self, capability: str, **params: Any) -> Any:
        if self._call_tool is None:
            raise MCPUnavailable(f"cannot serve {capability}: no MCP connection is configured")
        return self._call_tool(self.capabilities.tool_for(capability), params)

    # --- the read surface. Application code calls only these. ---------------

    def get_clock(self) -> Any:
        return self._invoke("get_clock")

    def get_account(self) -> Any:
        return self._invoke("get_account")

    def get_positions(self) -> Any:
        return self._invoke("get_positions")

    def get_open_orders(self) -> Any:
        return self._invoke("get_open_orders")

    def get_option_chain(self, underlying: str, **params: Any) -> Any:
        return self._invoke("get_option_chain", underlying=underlying, **params)

    def get_quote(self, symbol: str, **params: Any) -> Any:
        return self._invoke("get_quote", symbol=symbol, **params)

    def get_bars(self, symbol: str, **params: Any) -> Any:
        return self._invoke("get_bars", symbol=symbol, **params)
