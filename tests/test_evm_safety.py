"""Tests for the EVM honeypot / sellability safety layer."""

import os
from unittest.mock import AsyncMock

import pytest

from memecoin_alert_bot.data.evm_safety import EvmSafetyClient
from memecoin_alert_bot.engine import gates
from memecoin_alert_bot.engine.models import CoinData


def _coin(**kwargs) -> CoinData:
    values = dict(
        mint="0xToken", chain="robinhood", symbol="T", name="T",
        market_cap=50_000, volume_24h=10_000, liquidity=20_000,
    )
    values.update(kwargs)
    return CoinData(**values)


def test_sell_blocked_fails_sellability_gate():
    coin = _coin()
    coin.safety.sell_verdict = "sell_blocked"
    passed, results, _ = gates.evaluate_gates(coin)
    assert passed is False
    assert next(g for g in results if g.gate == "sellability").status == "failed"


def test_unverified_sell_caps_tier_but_does_not_fail():
    coin = _coin()
    coin.safety.sell_verdict = "unverified"
    coin.safety.risk_flags = []
    passed, results, unknown = gates.evaluate_gates(coin)
    assert passed is True          # not a hard failure
    assert unknown is True         # unknown critical gate caps tier at STANDARD
    assert next(g for g in results if g.gate == "sellability").status == "unknown"


def test_unverified_plus_blacklist_is_blocked_when_policy_on(monkeypatch):
    monkeypatch.setenv("EVM_BLOCK_ON_RISK", "true")
    coin = _coin()
    coin.safety.sell_verdict = "unverified"
    coin.safety.risk_flags = ["blacklist", "trading toggle"]
    passed, results, _ = gates.evaluate_gates(coin)
    assert passed is False
    detail = next(g for g in results if g.gate == "sellability").detail
    assert "risky contract" in detail


def test_require_verified_blocks_unverified(monkeypatch):
    monkeypatch.setenv("EVM_REQUIRE_SELL_VERIFIED", "true")
    coin = _coin()
    coin.safety.sell_verdict = "unverified"
    coin.safety.risk_flags = []
    passed, _, _ = gates.evaluate_gates(coin)
    assert passed is False


def test_verified_sellable_passes(monkeypatch):
    monkeypatch.setenv("EVM_REQUIRE_SELL_VERIFIED", "true")
    coin = _coin(pool_address="0xPair")
    coin.safety.sell_verdict = "verified_sellable"
    passed, _, _ = gates.evaluate_gates(coin)
    assert passed is True


@pytest.mark.asyncio
async def test_analyze_verdict_sell_blocked_on_revert():
    """A reverting holder→pair transfer must produce sell_blocked."""
    rpc = AsyncMock()
    rpc.get_block_number = AsyncMock(return_value=1000)
    rpc._rpc = AsyncMock(side_effect=[
        "0x" + "0" * 64,   # getPair (v2 #1) -> zero
        "0x" + "0" * 64,   # getPair (v2 #2) -> zero
        "0x" + "0" * 24 + "aa" * 20,  # getPool v3 fee 10000 -> pair
        "0x" + "f" * 64,   # eth_getCode (static scan)
        "0x" + hex(1000)[2:].rjust(64, "0"),  # totalSupply
    ])
    safety = EvmSafetyClient(rpc)
    # Directly exercise the simulation helper
    rpc._rpc = AsyncMock(return_value=None)  # revert
    ok = await safety.simulate_sell("0xToken", "0xPair", "0xHolder")
    assert ok is False
    rpc._rpc = AsyncMock(return_value="0x" + "0" * 63 + "1")
    ok = await safety.simulate_sell("0xToken", "0xPair", "0xHolder")
    assert ok is True


@pytest.mark.asyncio
async def test_static_flags_detects_blacklist():
    rpc = AsyncMock()
    from web3 import Web3

    sel = Web3.keccak(text="blacklist(address)")[:4].hex()
    rpc._rpc = AsyncMock(return_value="0x" + sel + "00" * 100)
    flags = await EvmSafetyClient(rpc).static_flags("0xToken")
    assert "blacklist" in flags
