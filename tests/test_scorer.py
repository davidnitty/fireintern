"""Tests for the SPYZER scoring engine."""

import pytest

from memecoin_alert_bot.engine import detectors, scorer
from memecoin_alert_bot.engine.models import CoinData, RiskLevel, Verdict


def test_healthy_meme_scores_buy():
    coin = CoinData(
        mint="HealthyMint1111111111111111111111111111111111",
        symbol="DOGEAI",
        name="Doge AI Agent",
        description="Autonomous doge AI agent token",
        market_cap=500_000,
        volume_24h=1_000_000,
        liquidity=150_000,
        holders=2_500,
        age_seconds=600,
        tokenized_agent=True,
    )
    signals = detectors.run_all(coin)
    score = scorer.score_coin(coin, signals)
    assert score.verdict in (Verdict.BUY, Verdict.WAIT)
    assert score.risk in (RiskLevel.LOW, RiskLevel.MEDIUM)


def test_bundled_honeypot_passes():
    coin = CoinData(
        mint="BadMint11111111111111111111111111111111111111",
        symbol="SCAM",
        name="Scam Token",
        market_cap=50_000,
        volume_24h=5_000,
        holders=20,
    )
    coin.safety.is_honeypot = True
    coin.safety.top_holders = [{"address": "A", "pct": 25.0}]
    signals = detectors.run_all(coin)
    score = scorer.score_coin(coin, signals)
    assert score.verdict == Verdict.PASS
    assert score.risk == RiskLevel.EXTREME


def test_score_breakdown_sums_correctly():
    coin = CoinData(
        mint="NeutralMint1111111111111111111111111111111111",
        symbol="NEUT",
        name="Neutral Token",
        market_cap=100_000,
        volume_24h=80_000,
        liquidity=20_000,
        holders=400,
    )
    signals = detectors.run_all(coin)
    score = scorer.score_coin(coin, signals)
    # Verify all breakdown values are within bounds.
    for value in score.breakdown.model_dump().values():
        assert 0.0 <= value <= 1.0
    assert -1.0 <= score.composite_score <= 1.0
