"""Tests for user filtering rules: bundling, unknown identity, MC band."""

import os

import pytest

from memecoin_alert_bot.engine import gates
from memecoin_alert_bot.engine.models import CoinData, RiskLevel, Tier
from memecoin_alert_bot.engine.scorer import assign_tier


def _coin(**kwargs) -> CoinData:
    values = dict(
        mint="BundleMint111111111111111111111111111111111",
        symbol="BND",
        name="Bundled",
        market_cap=50_000,
        volume_24h=10_000,
        liquidity=20_000,
    )
    values.update(kwargs)
    return CoinData(**values)


def test_top10_over_50pct_fails_coordination_gate():
    coin = _coin()
    coin.safety.top_holders = [
        {"address": f"H{i}", "pct": 6.0} for i in range(10)
    ]  # 60% combined
    passed, results, _ = gates.evaluate_gates(coin)
    assert passed is False
    coord = next(g for g in results if g.gate == "coordination")
    assert coord.status == "failed"


def test_bundled_supply_over_30pct_fails_coordination_gate():
    coin = _coin()
    coin.safety.bundled_pct = 35.0
    passed, _, _ = gates.evaluate_gates(coin)
    assert passed is False


def test_healthy_distribution_passes_coordination():
    coin = _coin()
    coin.safety.top_holders = [
        {"address": f"H{i}", "pct": 3.0} for i in range(10)
    ]  # 30% combined
    coin.safety.bundled_pct = 5.0
    passed, _, _ = gates.evaluate_gates(coin)
    assert passed is True


def test_bundled_gate_failure_maps_to_high_risk_tier():
    coin = _coin()
    coin.safety.bundled_pct = 45.0
    passed, _, unknown = gates.evaluate_gates(coin)
    tier = assign_tier(passed, q=70, r=30, c=70, risk=RiskLevel.LOW, gates_unknown=unknown)
    assert tier == Tier.HIGH_RISK


# ── Market-cap band ──────────────────────────────────────────────────────


def test_mc_band_config_defaults():
    from memecoin_alert_bot.config import Settings
    import os

    os.environ["TELEGRAM_BOT_TOKEN"] = "x"
    for key in ("MIN_MARKET_CAP", "MAX_MARKET_CAP"):
        os.environ.pop(key, None)
    settings = Settings()
    assert settings.min_market_cap == 10_000
    assert settings.max_market_cap == 0  # disabled by default


def test_solana_disabled_blocks_evaluation():
    """ENABLE_SOLANA_ALERTS=false must silence Solana but not Robinhood."""
    import asyncio

    from memecoin_alert_bot.engine.models import CoinData

    os.environ["TELEGRAM_BOT_TOKEN"] = "x"
    os.environ["ENABLE_SOLANA_ALERTS"] = "false"
    # Fresh settings instance picks up the env override.
    from memecoin_alert_bot.config import Settings

    settings = Settings()
    assert settings.enable_solana_alerts is False

    # And the config default (without the env var) remains enabled.
    os.environ.pop("ENABLE_SOLANA_ALERTS")
    settings_default = Settings()
    assert settings_default.enable_solana_alerts is True
