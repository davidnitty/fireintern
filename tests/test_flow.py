"""Tests for verified short-window flow metrics and normalizer preservation."""

from memecoin_alert_bot.engine.flow import compute_flow_metrics, score_volume_velocity
from memecoin_alert_bot.engine.models import CoinData
from memecoin_alert_bot.engine.normalizer import merge_enrichment


def make_verified_coin(**kwargs) -> CoinData:
    values = dict(
        mint="FlowMint11111111111111111111111111111111111",
        liquidity=10_000,
        volume_1h=30_000,
        volume_5m=5_000,
        buys_1h=120,
        sells_1h=80,
        buys_5m=15,
        sells_5m=5,
        price_change_5m=2.0,
        flow_data_quality="verified_usd",
    )
    values.update(kwargs)
    return CoinData(**values)


def test_verified_flow_computes_vl_acceleration_and_label():
    coin = make_verified_coin()
    compute_flow_metrics(coin)
    assert coin.vl_ratio_1h == 3.0
    assert coin.flow_ratio == 2.0  # 5k / (30k / 12)
    assert coin.swap_speed is not None and coin.swap_speed > 1
    assert coin.flow_label.startswith("🔥📈")
    assert score_volume_velocity(coin) > 0.5


def test_low_sample_flow_stays_neutral():
    coin = make_verified_coin(buys_5m=1, sells_5m=1)
    compute_flow_metrics(coin)
    assert coin.vl_ratio_1h is None
    assert coin.flow_label == "🔄?"
    assert score_volume_velocity(coin) == 0.5


def test_unknown_unit_flow_cannot_boost_score():
    coin = make_verified_coin(flow_data_quality="directional_only")
    compute_flow_metrics(coin)
    assert coin.flow_label == "-"
    assert score_volume_velocity(coin) == 0.5


def test_negative_price_disagreement_downgrades_velocity():
    coin = make_verified_coin(price_change_5m=-3.0)
    compute_flow_metrics(coin)
    assert "📉" in coin.flow_label
    assert score_volume_velocity(coin) < 0.7


def test_normalizer_preserves_flow_fields():
    coin = CoinData(mint="NormFlow1111111111111111111111111111111111")
    enriched = merge_enrichment(
        coin,
        {
            "volume_5m": 1000.0,
            "volume_1h": 6000.0,
            "buys_5m": 12,
            "sells_5m": 4,
            "price_change_5m": 1.3,
            "flow_data_quality": "verified_usd",
        },
    )
    assert enriched.volume_5m == 1000.0
    assert enriched.volume_1h == 6000.0
    assert enriched.buys_5m == 12
    assert enriched.sells_5m == 4
    assert enriched.flow_data_quality == "verified_usd"
