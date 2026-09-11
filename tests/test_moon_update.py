"""Tests for cumulative moon-update feedback."""

import asyncio
import pytest

from memecoin_alert_bot.engine.models import Alert, CoinData
from memecoin_alert_bot.storage.sqlite import Storage
from memecoin_alert_bot.utils.helpers import next_moon_threshold


# ── Threshold progression (cumulative, doubles after first call) ─────────


def test_first_threshold_is_update_pct():
    assert next_moon_threshold(50, 1.0) == 1.5
    assert next_moon_threshold(25, 1.0) == 1.25
    assert next_moon_threshold(100, 1.0) == 2.0


def test_percent_mode_steps_by_pct():
    # 25% mode: 1.25 -> 1.5625 -> 1.953125 ...
    assert next_moon_threshold(25, 1.25, "percent") == pytest.approx(1.5625)
    assert next_moon_threshold(25, 1.5625, "percent") == pytest.approx(1.953125)


def test_ladder_mode_doubles():
    assert next_moon_threshold(50, 20.5, "ladder") == 41.0
    assert next_moon_threshold(50, 41.0, "ladder") == 82.0
    assert next_moon_threshold(50, 1.5, "ladder") == 3.0


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


@pytest.mark.asyncio
async def test_moon_baseline_rescue_persists(tmp_path):
    """A NULL baseline must be filled PERSISTENTLY (regression).

    If the rescue isn't persisted, every check re-anchors to the current
    value and cumulative stays 1.0X forever — no update ever fires.
    """
    storage = Storage(str(tmp_path / "rescue.db"))
    await storage.connect()
    try:
        mint = "RescueMint111111111111111111111111111111111"
        # Alert with no MC/price in payload → baseline row created with NULLs
        await storage.save_alert(Alert(coin=CoinData(mint=mint, symbol="RESC")))
        state = await storage.ensure_moon_state(mint, None, None)
        assert state["baseline_mc"] is None

        # Rescue with current values, persisted
        await storage.set_moon_baseline(mint, 50_000, None)
        state = await storage.get_moon_state(mint)
        assert state["baseline_mc"] == 50_000

        # A later 1.6X move must now compute correctly
        assert (80_000 / state["baseline_mc"]) >= 1.5

        # Rescue must never overwrite an existing baseline
        await storage.set_moon_baseline(mint, 999_999, None)
        state = await storage.get_moon_state(mint)
        assert state["baseline_mc"] == 50_000
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_moon_update_end_to_end_51k_to_404k(tmp_path, monkeypatch):
    """Full path: alert at $51k, live price $404k => 7.92X update sent."""
    import importlib

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "e2e.db"))
    monkeypatch.setenv("MOON_UPDATE_PCT", "50")

    from memecoin_alert_bot import config

    config._settings = None  # reset cached settings for this env

    import main as main_module

    importlib.reload(main_module)
    from memecoin_alert_bot.engine.models import Alert, CoinData

    class StubDex:
        async def enrich_coin(self, mint, base, chain="solana"):
            return {"market_cap": 404_000, "price": 0.000404}

    class StubTelegram:
        def __init__(self):
            self.sent = []

        async def send_moon_update(self, symbol, multiple, mc_from, mc_to, alert_id=None):
            self.sent.append((symbol, round(multiple, 2), mc_from, mc_to))
            return True

    app = main_module.BotApp()
    await app.storage.connect()
    try:
        app.dexscreener = StubDex()
        app.telegram = StubTelegram()

        mint = "0x3889d404800a7f9D752A736356a7CF298F7Ac2fb".lower()
        coin = CoinData(
            mint=mint, chain="robinhood", symbol="XCOIN",
            market_cap=51_000, volume_24h=10_000,
        )
        alert_id = await app.storage.save_alert(Alert(coin=coin))

        sent = await app._moon_check(
            alert_id=alert_id, mint=mint, chain="robinhood", symbol="XCOIN",
            mc_alert=51_000, price_alert=None,
        )
        assert sent is True
        assert app.telegram.sent == [("XCOIN", 7.92, 51_000, 404_000)]
    finally:
        await app.storage.close()
        config._settings = None


@pytest.mark.asyncio
async def test_moon_watch_loop_survives_and_fires(tmp_path, monkeypatch):
    """Regression: the watch LOOP must not die on its own sleep call.

    A stale `interval_seconds` reference in the loop's sleep crashed the
    task seconds after startup, so no update ever fired regardless of how
    far a token pumped (observed: $18k -> $1.02M with no update).
    """
    import importlib

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "loop.db"))
    monkeypatch.setenv("MOON_UPDATE_PCT", "50")

    from memecoin_alert_bot import config

    config._settings = None

    import main as main_module

    importlib.reload(main_module)
    from memecoin_alert_bot.engine.models import Alert, CoinData

    class StubDex:
        async def enrich_coin(self, mint, base, chain="solana"):
            return {"market_cap": 1_020_000, "price": 0.00102}

    class StubTelegram:
        def __init__(self):
            self.sent = []

        async def send_moon_update(self, symbol, multiple, mc_from, mc_to, alert_id=None):
            self.sent.append((symbol, round(multiple, 2), mc_from, mc_to))
            return True

    app = main_module.BotApp()
    await app.storage.connect()
    try:
        app.dexscreener = StubDex()
        app.telegram = StubTelegram()

        mint = "0xcfb30b23d93bcc055c243db715d3e2d1c2facf09"
        await app.storage.save_alert(
            Alert(coin=CoinData(mint=mint, chain="robinhood", symbol="XCOIN",
                                market_cap=18_000, volume_24h=9_000))
        )

        # Run the real loop briefly; it must survive multiple ticks.
        try:
            await asyncio.wait_for(
                app._moon_watch_loop(tick_seconds=1, window_minutes=4320), timeout=3
            )
        except asyncio.TimeoutError:
            pass  # expected: the loop runs forever

        assert app.telegram.sent == [("XCOIN", 56.67, 18_000.0, 1_020_000)]
    finally:
        await app.storage.close()
        config._settings = None
