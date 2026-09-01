"""Capability-mapping safety tests.

Regression cover for a real defect: substring matching mapped the READ
capability `get_open_orders` onto the WRITE tool `place_stock_order`, because
"order" is a substring of both. Reading the order book would have submitted an
order. No read capability may ever resolve to a mutating tool.

The tool list here is the genuine 72-tool inventory advertised by
alpaca-mcp-server 3.4.7, so these tests exercise real names.
"""

from __future__ import annotations

import pytest

from src.broker.alpaca_mcp import (
    REQUIRED_CAPABILITIES,
    WRITE_VERBS,
    discover_capabilities,
    is_write_tool,
)

#: Verbatim from the connected server.
LIVE_TOOLS = [
    "add_asset_to_watchlist_by_id", "cancel_all_orders", "cancel_order_by_id",
    "close_all_positions", "close_position", "create_locate", "create_watchlist",
    "delete_watchlist_by_id", "do_not_exercise_options_position",
    "exercise_options_position", "fetch_alpaca_doc", "get_account_activities",
    "get_account_activities_by_type", "get_account_config", "get_account_info",
    "get_all_assets", "get_all_positions", "get_alpaca_endpoint_docs", "get_asset",
    "get_calendar", "get_clock", "get_corporate_action_announcement",
    "get_corporate_action_announcements", "get_corporate_actions", "get_crypto_bars",
    "get_crypto_latest_bar", "get_crypto_latest_orderbook", "get_crypto_latest_quote",
    "get_crypto_latest_trade", "get_crypto_quotes", "get_crypto_snapshot",
    "get_crypto_trades", "get_fixed_income_latest_quotes", "get_locate",
    "get_locate_quotes", "get_locates", "get_market_movers", "get_most_active_stocks",
    "get_news", "get_open_position", "get_option_bars", "get_option_chain",
    "get_option_contract", "get_option_contracts", "get_option_exchange_codes",
    "get_option_latest_quote", "get_option_latest_trade", "get_option_snapshot",
    "get_option_trades", "get_order_by_client_id", "get_order_by_id", "get_orders",
    "get_portfolio_history", "get_stock_bars", "get_stock_latest_bar",
    "get_stock_latest_quote", "get_stock_latest_trade", "get_stock_quotes",
    "get_stock_snapshot", "get_stock_trades", "get_watchlist_by_id", "get_watchlists",
    "list_alpaca_api_endpoints", "place_crypto_order", "place_option_order",
    "place_stock_order", "remove_asset_from_watchlist_by_id", "replace_order_by_id",
    "search_alpaca_api_specs", "search_alpaca_docs", "update_account_config",
    "update_watchlist_by_id",
]

MUTATING = [
    "place_stock_order", "place_option_order", "place_crypto_order",
    "cancel_all_orders", "cancel_order_by_id", "close_all_positions", "close_position",
    "replace_order_by_id", "delete_watchlist_by_id", "create_watchlist",
    "update_account_config", "exercise_options_position",
    "do_not_exercise_options_position", "add_asset_to_watchlist_by_id",
    "remove_asset_from_watchlist_by_id",
]


@pytest.mark.parametrize("name", MUTATING)
def test_mutating_tools_are_recognised(name):
    assert is_write_tool(name)


@pytest.mark.parametrize(
    "name",
    ["get_orders", "get_clock", "get_all_positions", "get_option_chain",
     "get_option_latest_quote", "get_stock_bars", "get_account_info"],
)
def test_read_tools_are_not_flagged(name):
    assert not is_write_tool(name)


# --- the regression --------------------------------------------------------


def test_open_orders_never_maps_to_an_order_placing_tool():
    """The exact defect: reading orders must not resolve to placing one."""
    mapping = discover_capabilities(LIVE_TOOLS).mapping
    assert mapping["get_open_orders"] == "get_orders"
    assert "place" not in mapping["get_open_orders"]


def test_no_capability_maps_to_a_mutating_tool():
    mapping = discover_capabilities(LIVE_TOOLS).mapping
    for capability, tool in mapping.items():
        assert not is_write_tool(tool), f"{capability} -> {tool} is a write tool"


def test_no_capability_maps_to_a_mutating_tool_even_if_only_writes_exist():
    """With reads stripped out, capabilities go MISSING rather than mapping to writes."""
    capabilities = discover_capabilities(MUTATING)
    assert capabilities.mapping == {}
    assert set(capabilities.missing) == set(REQUIRED_CAPABILITIES)
    assert not capabilities.complete


# --- correctness of the live mapping ----------------------------------------


def test_live_server_satisfies_every_capability():
    assert discover_capabilities(LIVE_TOOLS).complete


@pytest.mark.parametrize(
    "capability,expected",
    [
        ("get_clock", "get_clock"),
        ("get_account", "get_account_info"),
        ("get_positions", "get_all_positions"),
        ("get_open_orders", "get_orders"),
        ("get_option_chain", "get_option_chain"),
        ("get_quote", "get_option_latest_quote"),
        ("get_bars", "get_stock_bars"),
    ],
)
def test_live_mapping_is_exact(capability, expected):
    assert discover_capabilities(LIVE_TOOLS).mapping[capability] == expected


def test_quotes_resolve_to_options_not_stocks():
    """This desk trades option spreads; a stock quote is the wrong instrument."""
    assert "option" in discover_capabilities(LIVE_TOOLS).mapping["get_quote"]


def test_exact_names_beat_substring_matches():
    """get_orders must win over get_order_by_id, which is also a substring match."""
    assert discover_capabilities(
        ["get_order_by_id", "get_orders", "get_order_by_client_id"]
    ).mapping["get_open_orders"] == "get_orders"


def test_mapping_is_deterministic():
    first = discover_capabilities(LIVE_TOOLS).mapping
    second = discover_capabilities(list(reversed(LIVE_TOOLS))).mapping
    assert first == second


def test_toolsets_filter_still_excludes_writes():
    capabilities = discover_capabilities(LIVE_TOOLS, toolsets_filter=["order"])
    for tool in capabilities.mapping.values():
        assert not is_write_tool(tool)


def test_write_verbs_cover_every_mutating_live_tool():
    """A new mutating tool appearing upstream must not slip through unnoticed."""
    unguarded = [t for t in MUTATING if not any(v in t for v in WRITE_VERBS)]
    assert unguarded == []


# --- MCP payload envelope ----------------------------------------------------


def test_security_envelope_is_unwrapped():
    """Alpaca wraps payloads as {_alpaca_mcp_security, data}; callers want data."""
    from src.broker.mcp_stdio import unwrap_envelope

    payload = {
        "_alpaca_mcp_security": {"trust": "untrusted_tool_output"},
        "data": {"account_number": "PA3Y88DE6VC4", "equity": "100000"},
    }
    assert unwrap_envelope(payload) == {"account_number": "PA3Y88DE6VC4", "equity": "100000"}


def test_unwrapping_leaves_a_bare_payload_alone():
    from src.broker.mcp_stdio import unwrap_envelope

    assert unwrap_envelope({"equity": "1"}) == {"equity": "1"}
    assert unwrap_envelope([1, 2]) == [1, 2]
    assert unwrap_envelope(None) is None


def test_partial_envelope_is_not_unwrapped():
    """Only unwrap when both marker keys are present, never on a stray 'data' key."""
    from src.broker.mcp_stdio import unwrap_envelope

    payload = {"data": {"x": 1}}
    assert unwrap_envelope(payload) == payload


def test_account_identity_uses_account_number_not_the_internal_uuid():
    """MODE and the hackathon both match on PA3Y88DE6VC4, not the UUID."""
    from src.journal import Journal
    from src.reconcile import build_account

    raw = {
        "id": "6d4e1e3e-215e-474c-8ad0-c1518dff3669",
        "account_number": "PA3Y88DE6VC4",
        "equity": "100000",
        "cash": "100000",
        "buying_power": "400000",
        "last_equity": "100000",
        "options_trading_level": 3,
    }
    account = build_account(raw, "PA3Y88DE6VC4", Journal("journal/_test_ident.jsonl"))
    assert account.account_id == "PA3Y88DE6VC4"
    assert account.options_level == 3
    assert account.equity == 100_000.0
    assert account.buying_power == 400_000.0


def test_read_bridge_refuses_mutating_tools():
    """Even a mis-mapped capability cannot place an order through the read path."""
    from src.broker.alpaca_mcp import MCPUnavailable
    from src.broker.mcp_stdio import MCPStdioBridge

    bridge = MCPStdioBridge()
    for tool in ("place_option_order", "cancel_all_orders", "close_all_positions"):
        with pytest.raises(MCPUnavailable, match="refusing to call mutating tool"):
            bridge.call_tool(tool, {})


def test_extract_unwraps_both_result_shapes():
    """Regression: the JSON-text branch once returned the envelope unwrapped."""
    from src.broker.mcp_stdio import _extract

    envelope = {
        "_alpaca_mcp_security": {"trust": "untrusted_tool_output"},
        "data": {"account_number": "PA3Y88DE6VC4"},
    }
    expected = {"account_number": "PA3Y88DE6VC4"}

    class Structured:
        structuredContent = envelope
        content = []

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class TextResult:
        structuredContent = None

        def __init__(self, payload):
            import json as _json

            self.content = [TextBlock(_json.dumps(payload))]

    assert _extract(Structured()) == expected
    assert _extract(TextResult(envelope)) == expected


def test_extract_preserves_a_non_json_error_message():
    from src.broker.mcp_stdio import _extract

    class TextBlock:
        text = "rate limit exceeded"

    class Result:
        structuredContent = None
        content = [TextBlock()]

    assert _extract(Result()) == "rate limit exceeded"


# --- collection and clock payload shapes -------------------------------------


def test_result_wrapped_collections_are_normalised():
    """Alpaca returns {"result": [...]}; iterating that dict yields its keys."""
    from src.reconcile import as_list

    assert as_list({"result": []}) == []
    assert as_list({"result": [{"symbol": "SPY"}]}) == [{"symbol": "SPY"}]
    assert as_list([{"symbol": "SPY"}]) == [{"symbol": "SPY"}]
    assert as_list(None) == []


def test_a_string_payload_is_an_error_not_a_collection():
    from src.reconcile import ReconcileError, as_list

    with pytest.raises(ReconcileError, match="expected a collection"):
        as_list("Error calling tool 'get_all_positions': HTTP 500")


def test_session_minutes_are_derived_from_next_close():
    """Reading a missing minutes field as 0 would block every entry silently."""
    from src.marketdata import market_clock

    class FakeMCP:
        def get_clock(self):
            return {
                "is_open": True,
                "timestamp": "2026-09-01T14:55:56-04:00",
                "next_close": "2026-09-01T16:00:00-04:00",
            }

    is_open, minutes = market_clock(FakeMCP())
    assert is_open
    assert minutes == 64


def test_closed_market_reports_zero_minutes():
    from src.marketdata import market_clock

    class FakeMCP:
        def get_clock(self):
            return {"is_open": False, "next_close": "2026-09-02T16:00:00-04:00"}

    assert market_clock(FakeMCP()) == (False, 0)
