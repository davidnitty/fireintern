"""Composite scoring engine based on the SPYZER framework."""

from __future__ import annotations

from memecoin_alert_bot.engine import gates
from memecoin_alert_bot.engine.flow import compute_flow_metrics, score_volume_velocity
from memecoin_alert_bot.engine.models import (
    CoinData,
    RiskLevel,
    ScoreBreakdown,
    ScoreResult,
    Signal,
    SignalType,
    Tier,
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
    "buying_pressure": 0.05,
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
    """Score narrative strength from NLP analysis and triggered signals."""
    value = coin.narrative_strength

    # If NLP hasn't set it yet (edge case), start from keyword richness.
    if value == 0.0:
        value = 0.3
        if coin.ai_keywords or coin.narrative_keywords:
            value += min(0.3, (len(coin.ai_keywords) + len(coin.narrative_keywords)) * 0.05)

    # Boost for each non-risk signal.
    positive_signals = [s for s in signals if s.signal_type not in (
        SignalType.BUNDLING_RISK, SignalType.VAMP_RISK
    )]
    value += min(0.3, sum(s.confidence for s in positive_signals) * 0.15)

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


def score_buying_pressure(coin: CoinData) -> float:
    """Score buying pressure from swap event analysis (0 = all sells, 1 = all buys)."""
    bp = coin.buy_pressure
    if bp is None:
        return 0.5  # unknown
    if bp > 0.8:
        return 0.9
    if bp > 0.6:
        return 0.7
    if bp > 0.4:
        return 0.5
    if bp > 0.2:
        return 0.3
    return 0.1


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


# ── Revised three-axis scores (guide §4) ─────────────────────────────────


def compute_quality(coin: CoinData, signals: list[Signal], p: float, l: float, h: float, n: float, velocity: float) -> float:
    """Quality (0-100): organic flow, liquidity, momentum, dev, holders, narrative.

    Velocity is capped at 10% and returns neutral when its data units are not
    verified. It cannot dominate Quality Q.
    """
    flow = p  # buying pressure
    momentum = min(1.0, coin.vol_mc_ratio / 1.0) if coin.vol_mc_ratio else 0.3
    q = (
        0.18 * flow
        + 0.20 * l
        + 0.12 * momentum
        + 0.15 * score_dev_wallet(coin)
        + 0.10 * h
        + 0.10 * n
        + 0.05 * min(1.0, len(signals) / 3)
        + 0.10 * velocity
    )
    return round(q * 100, 1)


def compute_risk(coin: CoinData, b: float, risk: RiskLevel) -> float:
    """Risk (0-100): authority, exit, liquidity, deployer, coordination."""
    authority = 0.0
    if coin.safety.mint_authority_enabled is True:
        authority += 0.4
    if coin.safety.freeze_authority_enabled is True:
        authority += 0.3
    exit_risk = 0.0 if coin.pool_address or coin.chain == "solana" else 0.5
    coordination = min(1.0, coin.safety.bundled_pct / 60)
    r = 0.35 * b + 0.25 * authority + 0.20 * exit_risk + 0.20 * coordination
    if risk == RiskLevel.EXTREME:
        r = max(r, 0.9)
    elif risk == RiskLevel.HIGH:
        r = max(r, 0.7)
    return round(min(1.0, r) * 100, 1)


def compute_confidence(coin: CoinData, gate_results: list) -> float:
    """Data confidence (0-100): freshness, source agreement, completeness."""
    completeness = sum(
        1 for v in (coin.market_cap, coin.volume_24h, coin.holders, coin.liquidity, coin.price) if v is not None
    ) / 5
    source_count = sum(1 for v in coin.sources.values() if v is not None)
    source_agreement = min(1.0, source_count / 3)
    freshness = 1.0
    if coin.age_seconds is not None and coin.age_seconds > gates.MAX_AGE_SECONDS:
        freshness = 0.4
    c = 0.5 * completeness + 0.3 * source_agreement + 0.2 * freshness
    return round(c * 100, 1)


def assign_tier(gates_passed: bool, q: float, r: float, c: float, risk: RiskLevel) -> Tier:
    """Map (gates, Q, R, C) to a relative tier (guide §4)."""
    if not gates_passed or risk == RiskLevel.EXTREME or r >= 80:
        return Tier.HIGH_RISK
    if q >= 60 and r <= 40 and c >= 60:
        return Tier.DIAMOND
    if q >= 40 and r <= 60 and c >= 40:
        return Tier.STANDARD
    return Tier.GAMBLE


def score_coin(coin: CoinData, signals: list[Signal]) -> ScoreResult:
    """Compute composite + revised Q/R/C scores, gates, tier, and verdict."""
    # Calculate short-window flow only after provider normalization. Unknown
    # or non-USD chain units receive neutral velocity, never a positive boost.
    compute_flow_metrics(coin)

    b = score_bundling(coin)
    d = score_dev_wallet(coin)
    n = score_narrative(coin, signals)
    l = score_liquidity(coin)
    m = score_market_conditions(coin)
    h = score_holders(coin)
    c = score_chart_structure(coin)
    p = score_buying_pressure(coin)
    velocity = score_volume_velocity(coin)

    composite = (
        b * WEIGHTS["bundling"]
        + d * WEIGHTS["dev_wallet"]
        + n * WEIGHTS["narrative"]
        + l * WEIGHTS["liquidity"]
        + m * WEIGHTS["market_conditions"]
        + h * WEIGHTS["holders"]
        + c * WEIGHTS["chart_structure"]
        + p * WEIGHTS["buying_pressure"]
    )

    risk = determine_risk(coin, b, signals)
    composite = max(-1.0, min(1.0, composite))

    # Hard gates run before any high-conviction label (guide §3.2).
    gates_passed, gate_results = gates.evaluate_gates(coin)

    quality = compute_quality(coin, signals, p, l, h, n, velocity)
    risk_score = compute_risk(coin, b, risk)
    confidence = compute_confidence(coin, gate_results)
    tier = assign_tier(gates_passed, quality, risk_score, confidence, risk)

    explanation = [
        f"Bundling risk {b:.2f} × {WEIGHTS['bundling']}",
        f"Dev wallet {d:.2f} × {WEIGHTS['dev_wallet']}",
        f"Narrative {n:.2f} × {WEIGHTS['narrative']}",
        f"Liquidity {l:.2f} × {WEIGHTS['liquidity']}",
        f"Market {m:.2f} × {WEIGHTS['market_conditions']}",
        f"Holders {h:.2f} × {WEIGHTS['holders']}",
        f"Chart {c:.2f} × {WEIGHTS['chart_structure']}",
        f"Buy pressure {p:.2f} × {WEIGHTS['buying_pressure']}",
    ]

    # Verdict kept for familiarity but derived from tier + composite.
    if tier == Tier.HIGH_RISK or risk == RiskLevel.EXTREME:
        verdict = Verdict.PASS
    elif composite > 0.5:
        verdict = Verdict.BUY
    elif composite > 0.2:
        verdict = Verdict.WAIT
    elif composite > 0:
        verdict = Verdict.DYOR
    else:
        verdict = Verdict.PASS

    return ScoreResult(
        composite_score=round(composite, 3),
        confidence=round(confidence / 100, 3),
        verdict=verdict,
        risk=risk,
        quality=quality,
        risk_score=risk_score,
        data_confidence=confidence,
        tier=tier,
        gates=gate_results,
        gates_passed=gates_passed,
        invalidation=gates.build_invalidation(coin),
        breakdown=ScoreBreakdown(
            bundling=round(b, 3),
            dev_wallet=round(d, 3),
            narrative=round(n, 3),
            liquidity=round(l, 3),
            market_conditions=round(m, 3),
            holders=round(h, 3),
            chart_structure=round(c, 3),
            buying_pressure=round(p, 3),
        ),
        explanation=explanation,
    )
