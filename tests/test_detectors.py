"""Tests for the seven signal detectors."""

import pytest

from memecoin_alert_bot.engine import detectors
from memecoin_alert_bot.engine.models import CoinData, SignalType


def test_ai_agent_detected():
    coin = CoinData(
        mint="TestMint111111111111111111111111111111111111",
        name="AutoTrader AI Agent",
        description="Autonomous AI trading agent token",
        tokenized_agent=True,
    )
    sigs = detectors.run_all(coin)
    types = {s.signal_type for s in sigs}
    assert SignalType.AI_AGENT in types


def test_celebrity_detected():
    coin = CoinData(
        mint="TestMint222222222222222222222222222222222222",
        name="Trump Coin",
        description="Official celebrity token",
        market_cap=250_000,
        volume_24h=500_000,
    )
    sigs = detectors.run_all(coin)
    types = {s.signal_type for s in sigs}
    assert SignalType.CELEBRITY in types


def test_bundling_risk_detected():
    coin = CoinData(
        mint="TestMint333333333333333333333333333333333333",
        name="Bundled Token",
        market_cap=100_000,
        volume_24h=10_000,
    )
    coin.safety.top_holders = [
        {"address": "A", "pct": 8.0, "is_fresh": True},
        {"address": "B", "pct": 7.0, "is_fresh": True},
        {"address": "C", "pct": 6.0, "is_fresh": True},
    ]
    sigs = detectors.run_all(coin)
    types = {s.signal_type for s in sigs}
    assert SignalType.BUNDLING_RISK in types


def test_no_signal_for_blank_coin():
    coin = CoinData(
        mint="TestMint444444444444444444444444444444444444",
        name="Random",
        description="Nothing special",
    )
    sigs = detectors.run_all(coin)
    assert len(sigs) == 0
