"""Broker boundary tests (spec sections 4, 5, 6, 33).

The point of these tests is that a missing gate HALTS. A machine without the
Alpaca CLI installed, or without an MCP server connected, must refuse to trade
rather than improvise.
"""

from __future__ import annotations

import json

import pytest

from src.broker.alpaca_cli import (
    REQUIRED_CAPTURES,
    AlpacaCLI,
    CLIUnavailable,
    load_cli_reference,
    verify_cli,
)
from src.broker.alpaca_mcp import (
    REQUIRED_CAPABILITIES,
    AlpacaMCP,
    MCPTool,
    MCPUnavailable,
    discover_capabilities,
    load_tool_manifest,
    write_tool_manifest,
)
from src.safety import ExecutionMode, SafetyViolation
from src.types import RiskDecision

from .conftest import make_ticket

DEV_ENV = {"MODE": "development", "DEV_ACCOUNT_ID": "DEV-1", "JUDGE_ACCOUNT_ID": "JUDGE-9"}
PAPER = "https://paper-api.alpaca.markets"

FULL_TOOLSET = [
    "get_market_clock",
    "get_account_info",
    "get_all_positions",
    "list_orders",
    "get_option_chain",
    "get_latest_quote",
    "get_stock_bars",
]


# --- MCP capability discovery (spec section 5) ------------------------------


def test_discovery_maps_every_capability_on_a_complete_server():
    capabilities = discover_capabilities(FULL_TOOLSET)
    assert capabilities.complete
    assert set(capabilities.mapping) == set(REQUIRED_CAPABILITIES)
    assert capabilities.mapping["get_clock"] == "get_market_clock"
    assert capabilities.mapping["get_option_chain"] == "get_option_chain"


def test_discovery_reports_missing_capabilities():
    capabilities = discover_capabilities(["get_account_info", "get_latest_quote"])
    assert not capabilities.complete
    assert "get_option_chain" in capabilities.missing
    assert "get_bars" in capabilities.missing


def test_verify_halts_when_a_capability_is_missing():
    """Spec section 5: HALT at startup, not at trading time."""
    partial = discover_capabilities(["get_account_info"])
    mcp = AlpacaMCP(call_tool=lambda name, params: None, capabilities=partial)
    with pytest.raises(MCPUnavailable, match="missing required capabilities"):
        mcp.verify()


def test_verify_halts_without_a_connection():
    mcp = AlpacaMCP(call_tool=None, capabilities=discover_capabilities(FULL_TOOLSET))
    with pytest.raises(MCPUnavailable, match="no MCP connection"):
        mcp.verify()


def test_verify_passes_on_a_complete_connected_server():
    mcp = AlpacaMCP(lambda name, params: None, discover_capabilities(FULL_TOOLSET))
    mcp.verify()


def test_no_tool_name_is_hardcoded_in_the_read_surface():
    """The adapter must call whatever the server exposes, not a v1 name."""
    calls = []
    renamed = [
        "clock_v2",
        "account_v2",
        "positions_v2",
        "orders_v2",
        "option_chain_v2",
        "quote_v2",
        "bars_v2",
    ]
    mcp = AlpacaMCP(
        lambda name, params: calls.append(name),
        discover_capabilities(renamed),
    )
    mcp.get_clock()
    mcp.get_option_chain("SPY")
    assert calls == ["clock_v2", "option_chain_v2"]


def test_toolsets_filter_is_honoured():
    """ALPACA_TOOLSETS narrowing must be reflected in discovery."""
    capabilities = discover_capabilities(FULL_TOOLSET, toolsets_filter=["account", "position"])
    assert "get_account" in capabilities.mapping
    assert "get_option_chain" in capabilities.missing


def test_unmapped_capability_raises_rather_than_guessing():
    capabilities = discover_capabilities(["get_account_info"])
    with pytest.raises(MCPUnavailable, match="no MCP tool is mapped"):
        capabilities.tool_for("get_option_chain")


def test_manifest_round_trips(tmp_path):
    capabilities = discover_capabilities(FULL_TOOLSET)
    path = write_tool_manifest(capabilities, tmp_path / "mcp-tools.json")
    restored = load_tool_manifest(path)
    assert restored is not None
    assert restored.mapping == capabilities.mapping


def test_shipped_manifest_is_still_a_placeholder():
    """No MCP server has been connected yet, so the manifest must be empty."""
    assert load_tool_manifest("docs/mcp-tools.json") is None


# --- CLI gate (spec section 4) ----------------------------------------------


def test_shipped_cli_reference_is_captured_and_usable():
    """Captured from the installed binary on 2026-09-01, so the write path may unlock."""
    reference = load_cli_reference("docs/cli-reference.txt")
    assert not reference.is_placeholder
    assert reference.missing_captures == ()
    assert reference.is_usable


def test_captured_reference_carries_the_real_submit_flags():
    """Guards against the file being replaced by something that is not the real schema."""
    text = load_cli_reference("docs/cli-reference.txt").text
    for flag in ("--order-class", "--legs", "--qty", "--type", "--limit-price", "--time-in-force"):
        assert flag in text, flag
    assert "mleg" in text


def test_verify_cli_halts_when_the_binary_is_absent():
    with pytest.raises(CLIUnavailable, match="not on PATH"):
        verify_cli(binary="definitely-not-a-real-binary-xyz")


def test_reference_with_missing_captures_is_unusable(tmp_path):
    path = tmp_path / "cli-reference.txt"
    path.write_text("alpaca doctor\nall good\n", encoding="utf-8")
    reference = load_cli_reference(path)
    assert not reference.is_usable
    assert "alpaca order submit --schema" in reference.missing_captures


def test_reference_with_every_capture_is_usable(tmp_path):
    path = tmp_path / "cli-reference.txt"
    path.write_text("\n".join(REQUIRED_CAPTURES) + "\noutput...\n", encoding="utf-8")
    assert load_cli_reference(path).is_usable


def test_missing_reference_file_raises(tmp_path):
    with pytest.raises(CLIUnavailable, match="does not exist"):
        load_cli_reference(tmp_path / "absent.txt")


# --- write preflight (spec section 33) --------------------------------------


def cli(reference=None):
    return AlpacaCLI(
        execution_mode=ExecutionMode.from_env(DEV_ENV),
        trading_host=PAPER,
        reference=reference,
    )


def allow(lots: int = 1) -> RiskDecision:
    return RiskDecision(decision="ALLOW", reasons=(), allowed_lots=lots)


def deny() -> RiskDecision:
    return RiskDecision(decision="DENY", reasons=("market_closed",), allowed_lots=0)


def test_a_denied_ticket_can_never_reach_a_command():
    """No ALLOW means no broker command may be constructed."""
    with pytest.raises(SafetyViolation, match="was not allowed"):
        cli().preflight(make_ticket(), deny(), "DEV-1", "READY")


def test_zero_lots_can_never_reach_a_command():
    with pytest.raises(SafetyViolation, match="zero lots"):
        cli().preflight(make_ticket(), allow(lots=0), "DEV-1", "READY")


def test_a_halted_system_can_never_write():
    with pytest.raises(SafetyViolation, match="may not write"):
        cli().preflight(make_ticket(), allow(), "DEV-1", "HALTED")


def test_a_booting_system_can_never_write():
    with pytest.raises(SafetyViolation, match="may not write"):
        cli().preflight(make_ticket(), allow(), "DEV-1", "BOOTING")


def test_wrong_account_can_never_write(tmp_path):
    path = tmp_path / "ref.txt"
    path.write_text("\n".join(REQUIRED_CAPTURES), encoding="utf-8")
    with pytest.raises(SafetyViolation, match="account assertion failed"):
        cli(load_cli_reference(path)).preflight(make_ticket(), allow(), "JUDGE-9", "READY")


def test_uncaptured_schema_blocks_the_write_path():
    with pytest.raises(CLIUnavailable, match="refusing to construct"):
        cli().preflight(make_ticket(), allow(), "DEV-1", "READY")


def test_sending_halts_until_the_legs_format_is_proven(tmp_path):
    """Spec section 4: no CLI argument may be implemented from memory.

    The captured help documents --legs only as "list of order legs (<= 4)" and
    says nothing about its wire format. A command may therefore be BUILT and
    inspected, but never SENT, until --dry-run proves the encoding against the
    installed binary.
    """
    from src.broker.alpaca_cli import LegsFormatUnverified

    path = tmp_path / "ref.txt"
    path.write_text("\n".join(REQUIRED_CAPTURES), encoding="utf-8")
    desk = AlpacaCLI(
        execution_mode=ExecutionMode.from_env(DEV_ENV),
        trading_host=PAPER,
        reference=load_cli_reference(path),
        runner=lambda argv: (0, "{}", ""),
    )
    command = desk.build_submit_command(make_ticket(), allow(), "DEV-1")
    with pytest.raises(LegsFormatUnverified):
        desk.submit(command, "DEV-1")


def test_a_live_host_blocks_the_write_path(tmp_path):
    path = tmp_path / "ref.txt"
    path.write_text("\n".join(REQUIRED_CAPTURES), encoding="utf-8")
    live = AlpacaCLI(
        execution_mode=ExecutionMode.from_env(DEV_ENV),
        trading_host="https://api.alpaca.markets",
        reference=load_cli_reference(path),
    )
    with pytest.raises(SafetyViolation):
        live.preflight(make_ticket(), allow(), "DEV-1", "READY")


# --- architectural boundary (spec section 6) --------------------------------


def test_no_direct_broker_access_outside_the_adapters():
    """Only src/broker/alpaca_mcp.py and alpaca_cli.py may touch the broker."""
    from pathlib import Path

    forbidden_imports = ("alpaca_py", "alpaca_trade_api", "alpaca.trading", "alpaca.data")
    allowed = {"alpaca_mcp.py", "alpaca_cli.py"}

    offenders = []
    for path in Path("src").rglob("*.py"):
        if path.name in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in forbidden_imports:
            if needle in text:
                offenders.append(f"{path}: imports {needle}")
        for line in text.splitlines():
            lowered = line.lower()
            if "alpaca.markets" in lowered and "safetyviolation" not in lowered:
                if any(verb in lowered for verb in ("post(", "get(", "put(", "request(")):
                    offenders.append(f"{path}: direct HTTP to Alpaca -> {line.strip()}")
    assert not offenders, f"direct broker access outside the adapters: {offenders}"


def test_only_the_cli_adapter_shells_out_to_the_alpaca_binary():
    """Writes go through the CLI adapter, never a subprocess started elsewhere."""
    from pathlib import Path

    offenders = [
        str(path)
        for path in Path("src").rglob("*.py")
        if path.name != "alpaca_cli.py" and "subprocess" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"subprocess use outside the CLI adapter: {offenders}"


def test_paper_host_is_the_only_host_in_the_repo():
    """The live endpoint must not appear as a usable default anywhere."""
    from pathlib import Path

    offenders = []
    for path in list(Path("src").rglob("*.py")) + [Path(".env.example")]:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "//api.alpaca.markets" in line and "SafetyViolation" not in line:
                offenders.append(f"{path}: {line.strip()}")
    assert not offenders, offenders
