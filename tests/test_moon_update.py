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


@pytest.mark.asyncio
async def test_alert_message_ids_stored_per_chat(tmp_path):
    """Message ids persist per chat so moon updates can reply to the alert."""
    storage = Storage(str(tmp_path / "msgs.db"))
    await storage.connect()
    try:
        coin = CoinData(mint="ReplyMint111111111111111111111111111111111", symbol="RP")
        alert_id = await storage.save_alert(Alert(coin=coin))

        await storage.store_alert_message(alert_id, "-100AAA", 111)
        await storage.store_alert_message(alert_id, "-100BBB", 222)
        # Overwrite same chat (edited/re-sent) keeps one row per chat.
        await storage.store_alert_message(alert_id, "-100AAA", 333)

        ids = await storage.get_alert_message_ids(alert_id)
        assert ids == {"-100AAA": 333, "-100BBB": 222}
    finally:
        await storage.close()


def test_maestro_deep_link_contains_referral_and_ca():
    """Maestro button URL = t.me/maestro?start=<referral>-<CA>."""
    coin = CoinData(
        mint="0x5d9144d2d017386519a7134Fcc7f1E4bA22f920c", symbol="SHRUB", name="Lil' Shrub"
    )
    from memecoin_alert_bot.bot.formatter import _maestro_url

    url = _maestro_url(coin)
    assert url == (
        "https://t.me/maestro?start=r-nittyberry0-"
        "0x5d9144d2d017386519a7134Fcc7f1E4bA22f920c"
    )


def test_bloom_and_based_urls_embed_the_ca():
    """Bloom/Based buttons must carry the token CA in the start payload."""
    coin = CoinData(
        mint="0x5d9144d2d017386519a7134Fcc7f1E4bA22f920c", symbol="SHRUB"
    )
    from memecoin_alert_bot.bot.formatter import _based_bot_url, _bloom_url

    bloom = _bloom_url(coin)
    based = _based_bot_url(coin)
    assert "ref_5I0QKYENJB_0x5d9144d2d017386519a7134Fcc7f1E4bA22f920c" in bloom
    assert "r_nittyberry0_0x5d9144d2d017386519a7134Fcc7f1E4bA22f920c" in based


def test_moon_check_interval_decays_with_age():
    from memecoin_alert_bot.utils.helpers import moon_check_interval_minutes

    assert moon_check_interval_minutes(5) == 0.5      # fresh: every 30s
    assert moon_check_interval_minutes(45) == 2.0     # 30m-2h: every 2 min
    assert moon_check_interval_minutes(300) == 10.0   # up to 24h: every 10 min
    assert moon_check_interval_minutes(1400) == 10.0


@pytest.mark.asyncio
async def test_has_alerted_persists(tmp_path):
    """Once-per-mint: the alerts table remembers every called mint."""
    storage = Storage(str(tmp_path / "once.db"))
    await storage.connect()
    try:
        mint = "OnceMint1111111111111111111111111111111111111"
        assert await storage.has_alerted(mint) is False
        await storage.save_alert(Alert(coin=CoinData(mint=mint, symbol="ONCE")))
        assert await storage.has_alerted(mint) is True
        # Still true "after restart" (new connection, same file).
        await storage.close()
        storage2 = Storage(str(tmp_path / "once.db"))
        await storage2.connect()
        try:
            assert await storage2.has_alerted(mint) is True
        finally:
            await storage2.close()
        return
    except Exception:
        await storage.close()
        raise
