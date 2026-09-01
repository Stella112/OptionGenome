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
#: Verbs that identify a tool as MUTATING. A read capability may never map to
#: one of these, whatever its name looks like.
#:
#: This guard exists because substring matching is not safe here: "order" is a
#: substring of both `get_orders` and `place_stock_order`, and an earlier version
#: of this module mapped the read capability `get_open_orders` onto
#: `place_stock_order`. Reading the order book would have submitted an order.
WRITE_VERBS: tuple[str, ...] = (
    "place_",
    "submit_",
    "cancel_",
    "close_",
    "replace_",
    "delete_",
    "create_",
    "update_",
    "add_",
    "remove_",
    "exercise_",
    "do_not_exercise",
    "liquidate",
)


def is_write_tool(name: str) -> bool:
    """True when a tool name indicates it mutates broker state."""
    lowered = name.lower()
    return any(verb in lowered for verb in WRITE_VERBS)


#: Ordered preference per capability: exact tool names first, most specific
#: first. Substring fragments are a last resort, and are still write-guarded.
#:
#: `get_quote` deliberately prefers OPTION quotes: this desk trades option
#: spreads, so a stock quote is the wrong instrument, not merely a worse match.
REQUIRED_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "get_clock": ("get_clock", "get_market_clock", "clock"),
    "get_account": ("get_account_info", "get_account", "account"),
    "get_positions": ("get_all_positions", "get_positions", "position"),
    "get_open_orders": ("get_orders", "list_orders", "get_open_orders"),
    "get_option_chain": ("get_option_chain", "get_option_contracts", "option_chain"),
    "get_quote": (
        "get_option_latest_quote",
        "get_option_snapshot",
        "get_latest_quote",
        "get_stock_latest_quote",
    ),
    "get_bars": ("get_stock_bars", "get_bars", "stock_bars", "bar"),
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

    # Every required capability is READ-ONLY, so mutating tools are removed from
    # consideration entirely rather than merely ranked lower.
    readable = [t for t in normalised if not is_write_tool(t.name)]

    mapping: dict[str, str] = {}
    missing: list[str] = []
    for capability, candidates in required.items():
        by_name = {t.name.lower(): t.name for t in readable}
        # 1. exact tool name, in declared preference order
        match = next((by_name[c.lower()] for c in candidates if c.lower() in by_name), None)
        # 2. substring fallback, still restricted to non-mutating tools
        if match is None:
            match = next(
                (t.name for fragment in candidates for t in readable if t.matches(fragment)),
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

    # Parameter names below are transcribed from the connected server's own tool
    # schemas (docs/mcp-schemas.txt, regenerate with scripts/mcp_schemas.py), not
    # from memory. Passing `underlying` instead of `underlying_symbol` left the
    # URL template unsubstituted and Alpaca rejected the literal placeholder.

    def get_option_chain(self, underlying: str, **params: Any) -> Any:
        """Option chain for one underlying.

        Accepts the server's filters, notably expiration_date_gte /
        expiration_date_lte and type, so the DTE window can be narrowed at the
        source rather than pulling the whole chain.
        """
        return self._invoke("get_option_chain", underlying_symbol=underlying, **params)

    def get_quote(self, symbol: str | Sequence[str], **params: Any) -> Any:
        """Latest quote(s). The tool takes `symbols` and accepts several at once."""
        symbols = symbol if isinstance(symbol, str) else ",".join(symbol)
        return self._invoke("get_quote", symbols=symbols, **params)

    def get_bars(self, symbol: str | Sequence[str], **params: Any) -> Any:
        """Historical bars. `symbols` is required; timeframe/days are optional."""
        symbols = symbol if isinstance(symbol, str) else ",".join(symbol)
        return self._invoke("get_bars", symbols=symbols, **params)
