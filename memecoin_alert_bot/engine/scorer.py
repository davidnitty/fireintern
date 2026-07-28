"""Composite scoring engine based on the SPYZER framework."""

from __future__ import annotations

from memecoin_alert_bot.engine.models import (
    CoinData,
    RiskLevel,
    ScoreBreakdown,
    ScoreResult,
    Signal,
    SignalType,
    Verdict,
)

# SPYZER weights from the project plan
WEIGHTS = {
    "bundling": -0.30,
    "dev_wallet": 0.15,
    "narrative": 0.25,
    "liquidity": 0.20,
    "market_conditions": 0.10,
    "holders": 0.10,
    "chart_structure": 0.10,
}


def score_bundling(coin: CoinData) -> float:
    """Return normalized bundling risk factor (0 = safe, 1 = extreme)."""
    factors = []

    top = coin.top_holder_pct
    if top > 3.5:
        factors.append(min(1.0, (top - 3.5) / 10.0))

    top10 = coin.top10_holder_pct
    if top10 > 50:
        factors.append(min(1.0, (top10 - 50) / 40.0))

    vol_mc = coin.vol_mc_ratio
    if 0 < vol_mc < 0.8:
        factors.append(min(1.0, (0.8 - vol_mc) / 0.8))

    bundled = coin.safety.bundled_pct
    if bundled > 10:
        factors.append(min(1.0, (bundled - 10) / 40.0))

    fresh_count = sum(1 for h in coin.safety.top_holders if h.is_fresh)
    if fresh_count >= 3:
        factors.append(min(1.0, fresh_count / 5.0))

    if coin.safety.rugcheck_score is not None and coin.safety.rugcheck_score > 500:
        factors.append(min(1.0, (coin.safety.rugcheck_score - 500) / 1500.0))

    if coin.safety.is_honeypot:
        factors.append(1.0)

    if not factors:
        return 0.0
    return min(1.0, max(factors) * 0.6 + sum(factors) / len(factors) * 0.4)


def score_dev_wallet(coin: CoinData) -> float:
    """Score developer wallet safety (0 = risky, 1 = benign)."""
    if not coin.dev_wallet:
        return 0.5

    # Basic heuristic: if mint authority is enabled, dev can still mint.
    if coin.safety.mint_authority_enabled:
        return 0.1
    if coin.safety.freeze_authority_enabled:
        return 0.3

    # Low balance after launch can mean dev rugged/left.
    if coin.dev_sol_balance is not None:
        if coin.dev_sol_balance < 0.05:
            return 0.2
        if coin.dev_sol_balance > 1.0:
            return 0.8

    # CTO narrative improves dev safety score.
    if "dev" in coin.description.lower() or "community takeover" in coin.description.lower():
        return 0.7

    return 0.5


def score_narrative(coin: CoinData, signals: list[Signal]) -> float:
    """Score narrative strength from triggered signals and keyword richness."""
    value = 0.3
    if coin.ai_keywords or coin.narrative_keywords:
        value += min(0.3, (len(coin.ai_keywords) + len(coin.narrative_keywords)) * 0.05)

    # Boost for each non-risk signal
    positive_signals = [s for s in signals if s.signal_type not in (
        SignalType.BUNDLING_RISK, SignalType.VAMP_RISK
    )]
    value += min(0.4, sum(s.confidence for s in positive_signals) * 0.2)

    return min(1.0, value)


def score_liquidity(coin: CoinData) -> float:
    """Score liquidity/TVL health."""
    if coin.liquidity is None:
        return 0.4
    if coin.liquidity < 1_000:
        return 0.1
    if coin.liquidity < 10_000:
        return 0.3
    if coin.liquidity < 100_000:
        return 0.6
    return 0.9


def score_market_conditions(coin: CoinData) -> float:
    """Score overall market microstructure."""
    value = 0.4

    if coin.market_cap and coin.market_cap > 0:
        # Too tiny may be illiquid; too large already moved.
        if 10_000 <= coin.market_cap <= 5_000_000:
            value += 0.3
        elif coin.market_cap > 50_000_000:
            value -= 0.2

    if coin.vol_mc_ratio:
        if coin.vol_mc_ratio > 1.0:
            value += 0.2
        elif coin.vol_mc_ratio < 0.2:
            value -= 0.2

    if coin.bonding_curve is not None:
        if 0.2 <= coin.bonding_curve <= 0.8:
            value += 0.1

    return max(0.0, min(1.0, value))


def score_holders(coin: CoinData) -> float:
    """Score holder distribution."""
    if coin.holders is None:
        return 0.4
    if coin.holders < 50:
        return 0.1
    if coin.holders < 300:
        return 0.3
    if coin.holders < 1_000:
        return 0.6
    return 0.85


def score_chart_structure(coin: CoinData) -> float:
    """Placeholder chart-structure score (requires OHLC data)."""
    # Without OHLC candles we use age/volume shape as a proxy.
    if coin.age_seconds is None or coin.age_seconds < 60:
        return 0.45
    if coin.vol_mc_ratio and coin.vol_mc_ratio > 0.5:
        return 0.7
    return 0.5


def determine_risk(coin: CoinData, bundling_score: float, signals: list[Signal]) -> RiskLevel:
    """Determine overall risk level."""
    if bundling_score >= 0.8 or coin.safety.is_honeypot:
        return RiskLevel.EXTREME

    risk_signals = [s for s in signals if s.signal_type == SignalType.BUNDLING_RISK]
    vamp_signals = [s for s in signals if s.signal_type == SignalType.VAMP_RISK]

    if risk_signals and max((s.confidence for s in risk_signals), default=0) > 0.75:
        return RiskLevel.EXTREME
    if bundling_score >= 0.6 or (vamp_signals and max((s.confidence for s in vamp_signals), default=0) > 0.7):
        return RiskLevel.HIGH
    if bundling_score >= 0.35 or len(risk_signals) > 0:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def score_coin(coin: CoinData, signals: list[Signal]) -> ScoreResult:
    """Compute the composite SPYZER score and produce a verdict."""
    b = score_bundling(coin)
    d = score_dev_wallet(coin)
    n = score_narrative(coin, signals)
    l = score_liquidity(coin)
    m = score_market_conditions(coin)
    h = score_holders(coin)
    c = score_chart_structure(coin)

    composite = (
        b * WEIGHTS["bundling"]
        + d * WEIGHTS["dev_wallet"]
        + n * WEIGHTS["narrative"]
        + l * WEIGHTS["liquidity"]
        + m * WEIGHTS["market_conditions"]
        + h * WEIGHTS["holders"]
        + c * WEIGHTS["chart_structure"]
    )

    risk = determine_risk(coin, b, signals)

    # Clamp between -1 and 1, then shift to 0-1 for confidence display.
    composite = max(-1.0, min(1.0, composite))

    explanation = [
        f"Bundling risk {b:.2f} × {WEIGHTS['bundling']}",
        f"Dev wallet {d:.2f} × {WEIGHTS['dev_wallet']}",
        f"Narrative {n:.2f} × {WEIGHTS['narrative']}",
        f"Liquidity {l:.2f} × {WEIGHTS['liquidity']}",
        f"Market {m:.2f} × {WEIGHTS['market_conditions']}",
        f"Holders {h:.2f} × {WEIGHTS['holders']}",
        f"Chart {c:.2f} × {WEIGHTS['chart_structure']}",
    ]

    # Risk override: EXTREME -> PASS regardless of score.
    if risk == RiskLevel.EXTREME:
        verdict = Verdict.PASS
    elif composite > 0.5:
        verdict = Verdict.BUY
    elif composite > 0.2:
        verdict = Verdict.WAIT
    elif composite > 0:
        verdict = Verdict.DYOR
    else:
        verdict = Verdict.PASS

    # Confidence derived from signal confidence and data completeness.
    signal_conf = sum(s.confidence for s in signals) / max(len(signals), 1)
    data_completeness = sum(
        1 for v in (coin.market_cap, coin.volume_24h, coin.holders, coin.liquidity) if v is not None
    ) / 4
    confidence = 0.5 * signal_conf + 0.5 * data_completeness

    return ScoreResult(
        composite_score=round(composite, 3),
        confidence=round(confidence, 3),
        verdict=verdict,
        risk=risk,
        breakdown=ScoreBreakdown(
            bundling=round(b, 3),
            dev_wallet=round(d, 3),
            narrative=round(n, 3),
            liquidity=round(l, 3),
            market_conditions=round(m, 3),
            holders=round(h, 3),
            chart_structure=round(c, 3),
        ),
        explanation=explanation,
    )
