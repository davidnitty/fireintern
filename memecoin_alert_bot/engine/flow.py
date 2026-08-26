"""Short-window flow acceleration and V/L analysis.

All quality-positive metrics require verified USD data from a source such as
DexScreener. Unknown, inferred, or pair-denominated chain values are never
allowed to inflate quality scores.
"""

from __future__ import annotations

from memecoin_alert_bot.engine.models import CoinData

MIN_TXNS_5M = 4
MIN_LIQUIDITY_USD = 1_000.0


def compute_flow_metrics(coin: CoinData) -> None:
    """Compute V/L, acceleration, swap speed, and labelled directional flow.

    Leaves values neutral/unknown if data is insufficient or not verified USD.
    """
    coin.vl_ratio_1h = None
    coin.flow_ratio = None
    coin.swap_speed = None
    coin.flow_label = "-"

    if coin.flow_data_quality != "verified_usd":
        return
    if not coin.liquidity or coin.liquidity < MIN_LIQUIDITY_USD:
        return
    if coin.volume_1h is None or coin.volume_5m is None:
        return

    buys_5m = coin.buys_5m or 0
    sells_5m = coin.sells_5m or 0
    buys_1h = coin.buys_1h or 0
    sells_1h = coin.sells_1h or 0
    txns_5m = buys_5m + sells_5m
    txns_1h = buys_1h + sells_1h

    # Sample-size guard: do not infer meaningful flow from a handful of swaps.
    if txns_5m < MIN_TXNS_5M or txns_1h <= 0:
        coin.flow_label = "🔄?"
        return

    coin.vl_ratio_1h = coin.volume_1h / coin.liquidity
    expected_m5 = coin.volume_1h / 12
    coin.flow_ratio = coin.volume_5m / expected_m5 if expected_m5 > 0 else None
    expected_swaps_5m = txns_1h / 12
    coin.swap_speed = txns_5m / expected_swaps_5m if expected_swaps_5m > 0 else None

    # Transaction-count buy pressure. DexScreener does not supply buy/sell USD
    # notional; never call this volume pressure.
    buy_ratio = buys_5m / txns_5m
    price_delta = coin.price_change_5m or 0.0
    if price_delta > 0.5 and buy_ratio > 0.55:
        direction = "📈"
    elif price_delta < -0.5:
        # Price down is adverse even if buy-count data is mixed; never label
        # a falling chart as positive flow.
        direction = "📉"
    else:
        direction = "🔄"

    ratio = coin.flow_ratio or 0.0
    if ratio > 1.2:
        heat = "🔥"
    elif ratio >= 0.8:
        heat = "🟢"
    elif ratio >= 0.5:
        heat = "🟡"
    else:
        heat = "🧊"
    coin.flow_label = f"{heat}{direction}{ratio:.1f}"


def score_volume_velocity(coin: CoinData) -> float:
    """Return guarded 0-1 flow score for Quality Q.

    High V/L is only positive with transaction sample, non-negative direction,
    verified USD data, and adequate liquidity. It cannot reward low-liquidity
    wash-like spikes or unknown-unit chain values.
    """
    if coin.flow_data_quality != "verified_usd":
        return 0.5
    if coin.vl_ratio_1h is None or coin.flow_ratio is None or coin.swap_speed is None:
        return 0.5

    # V/L is activity relative to available exit depth, not a universal
    # profitability threshold. These are conservative initial hypotheses.
    vl = coin.vl_ratio_1h
    if vl < 0.5:
        vl_score = 0.2
    elif vl <= 10.0:
        vl_score = 0.75
    else:
        vl_score = 0.55  # extreme velocity can be artificial or unstable

    flow = coin.flow_ratio
    if flow > 1.2:
        flow_score = 0.9
    elif flow >= 0.8:
        flow_score = 0.7
    elif flow >= 0.5:
        flow_score = 0.45
    else:
        flow_score = 0.2

    # Price-volume disagreement prevents a spike from being rewarded.
    if (coin.price_change_5m or 0) < -0.5:
        flow_score = min(flow_score, 0.25)

    # Transaction speed adds modest confirmation, capped.
    speed_score = min(1.0, coin.swap_speed / 1.2)
    return round(0.50 * vl_score + 0.35 * flow_score + 0.15 * speed_score, 3)
