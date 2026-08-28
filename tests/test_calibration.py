"""Tests for zero-alert fixes, v4 gate rules, and the calibration ledger."""

import pytest

from memecoin_alert_bot.engine import gates
from memecoin_alert_bot.engine.models import CoinData, Tier
from memecoin_alert_bot.engine.normalizer import create_from_pumpportal
from memecoin_alert_bot.engine.scorer import assign_tier
from memecoin_alert_bot.storage.sqlite import Storage


# ── SOL→USD unit fix ─────────────────────────────────────────────────────


def test_pumpportal_market_cap_converted_to_usd():
    event = {"mint": "MintSol", "symbol": "SOLT", "name": "Sol Test", "marketCapSol": 50}
    coin = create_from_pumpportal(event, sol_usd=170.0)
    assert coin.market_cap == 8500.0  # 50 SOL × $170


# ── Gate semantics: unknown ≠ failed, venue via liquidity ───────────────


def _coin(**kwargs) -> CoinData:
    values = dict(
        mint="GateMint1111111111111111111111111111111111111",
        market_cap=50_000,
        volume_24h=10_000,
    )
    values.update(kwargs)
    return CoinData(**values)


def test_unknown_authority_no_longer_fails_gates():
    coin = _coin()
    passed, results, unknown = gates.evaluate_gates(coin)
    assert passed is True            # unknown is not a failure
    assert unknown is True           # but caps the tier
    auth = next(g for g in results if g.gate == "authority")
    assert auth.status == "unknown"


def test_active_authority_still_fails_gates():
    coin = _coin()
    coin.safety.mint_authority_enabled = True
    passed, _, unknown = gates.evaluate_gates(coin)
    assert passed is False
    assert unknown is False


def test_v4_venue_passes_via_liquidity():
    """v4 tokens have pool_address=None; positive liquidity is venue evidence."""
    coin = _coin(pool_address=None, liquidity=77_600)
    passed, results, _ = gates.evaluate_gates(coin)
    assert passed is True
    for name in ("identity", "sellability"):
        g = next(g for g in results if g.gate == name)
        assert g.status == "passed", name


def test_no_venue_and_no_liquidity_fails():
    coin = _coin(chain="robinhood", pool_address=None, liquidity=None)
    passed, _, _ = gates.evaluate_gates(coin)
    assert passed is False


# ── Tier cap for unknown gates ───────────────────────────────────────────


def test_unknown_gate_caps_tier_at_standard():
    # Would qualify for DIAMOND on Q/R/C alone.
    tier = assign_tier(True, q=80, r=20, c=80, risk=__import__(
        "memecoin_alert_bot.engine.models", fromlist=["RiskLevel"]
    ).RiskLevel.LOW, gates_unknown=True)
    assert tier == Tier.STANDARD


def test_failed_gate_still_high_risk():
    tier = assign_tier(False, q=80, r=20, c=80, risk=__import__(
        "memecoin_alert_bot.engine.models", fromlist=["RiskLevel"]
    ).RiskLevel.LOW)
    assert tier == Tier.HIGH_RISK


# ── Calibration ledger storage ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_decisions_and_outcomes_ledger(tmp_path):
    storage = Storage(str(tmp_path / "ledger.db"))
    await storage.connect()
    try:
        await storage.record_decision("Mint1", "solana", "T1", "mc_below_floor", "mc 3000", 3000)
        await storage.record_decision("Mint2", "robinhood", "T2", "sent", score_json='{"tier":"DIAMOND"}')

        # Save an alert and verify outcome lifecycle.
        coin = CoinData(mint="Mint2", chain="robinhood", symbol="T2", price=0.001, market_cap=50_000)
        from memecoin_alert_bot.engine.models import Alert
        alert = Alert(coin=coin)
        await storage.save_alert(alert)

        # Nothing due immediately (horizons are in the future).
        due = await storage.get_alerts_without_outcomes([5])
        assert due == []

        # Force due by backdating the alert.
        await storage._connection.execute(
            "UPDATE alerts SET generated_at = ? WHERE mint = 'Mint2'",
            ("2026-01-01T00:00:00+00:00",),
        )
        await storage._connection.commit()
        due = await storage.get_alerts_without_outcomes([5, 15, 60, 1440])
        assert len(due) == 4  # one per horizon

        await storage.record_outcome(
            alert_id=due[0]["alert_id"], mint="Mint2", horizon_min=5,
            alert_at=due[0]["generated_at"],
            price_alert=0.001, price_horizon=0.0012,
            mc_alert=50_000, mc_horizon=60_000,
        )
        summary = await storage.calibration_summary()
        assert len(summary) == 1
        assert summary[0]["tier"] is None or isinstance(summary[0]["tier"], str)
        assert summary[0]["pct_change"] == pytest.approx(20.0, rel=0.01)
    finally:
        await storage.close()
