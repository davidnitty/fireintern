"""Tests for cumulative moon-update feedback."""

import pytest

from memecoin_alert_bot.engine.models import Alert, CoinData
from memecoin_alert_bot.storage.sqlite import Storage
from memecoin_alert_bot.utils.helpers import next_moon_threshold


# ── Threshold progression (cumulative, doubles after first call) ─────────


def test_first_threshold_is_update_pct():
    assert next_moon_threshold(50, 1.0) == 1.5
    assert next_moon_threshold(100, 1.0) == 2.0


def test_subsequent_thresholds_double_from_last_announced():
    assert next_moon_threshold(50, 20.5) == 41.0
    assert next_moon_threshold(50, 41.0) == 82.0
    assert next_moon_threshold(50, 1.5) == 3.0


@pytest.mark.asyncio
async def test_moon_state_anchors_to_earliest_alert(tmp_path):
    """Re-alerts during a pump must NOT reset the cumulative baseline."""
    storage = Storage(str(tmp_path / "cumulative.db"))
    await storage.connect()
    try:
        # Earliest alert at $50.1K (the original call).
        coin1 = CoinData(mint="MUMint11111111111111111111111111111111111", symbol="MU", market_cap=50_100)
        await storage.save_alert(Alert(coin=coin1))
        # Re-alert during the pump at $552.8K.
        coin2 = CoinData(mint="MUMint11111111111111111111111111111111111", symbol="MU", market_cap=552_800)
        await storage.save_alert(Alert(coin=coin2))

        # ensure_moon_state called from the SECOND alert's outcome must still
        # anchor to the earliest alert's baseline.
        state = await storage.ensure_moon_state(
            "MUMint11111111111111111111111111111111111", 552_800, None
        )
        assert state["baseline_mc"] == 50_100
        assert state["last_multiple"] == 1.0

        # Pump to $2.1M => cumulative 41.9X from the original call.
        cumulative = 2_100_000 / 50_100
        assert cumulative == pytest.approx(41.92, rel=0.01)
        assert cumulative >= next_moon_threshold(50, 20.5)

        await storage.set_moon_multiple(
            "MUMint11111111111111111111111111111111111", cumulative
        )
        state = await storage.get_moon_state("MUMint11111111111111111111111111111111111")
        assert state["last_multiple"] == pytest.approx(41.92, rel=0.01)
    finally:
        await storage.close()
