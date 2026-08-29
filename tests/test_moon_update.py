"""Tests for the moon-update price feedback feature."""

import pytest

from memecoin_alert_bot.engine.models import Alert, CoinData
from memecoin_alert_bot.storage.sqlite import Storage


@pytest.mark.asyncio
async def test_moon_update_sent_once_per_alert(tmp_path):
    storage = Storage(str(tmp_path / "moon.db"))
    await storage.connect()
    try:
        coin = CoinData(mint="MoonMint111111111111111111111111111111111", symbol="FROGGY")
        await storage.save_alert(Alert(coin=coin))
        row = (await storage._connection.execute_fetchall(
            "SELECT id FROM alerts WHERE mint = 'MoonMint111111111111111111111111111111111'"
        ))[0]
        alert_id = row["id"]

        assert await storage.moon_update_sent(alert_id) is False

        await storage.record_outcome(
            alert_id=alert_id, mint="MoonMint111111111111111111111111111111111",
            horizon_min=5, alert_at="2026-01-01T00:00:00+00:00",
            price_alert=0.001, price_horizon=0.004,
            mc_alert=11_700, mc_horizon=46_800,
        )
        await storage.mark_moon_update_sent(alert_id)
        assert await storage.moon_update_sent(alert_id) is True
    finally:
        await storage.close()


def test_moon_update_message_format():
    """Message must match the sample layout exactly."""
    symbol, multiple = "FROGGY", 31.5
    mc_from, mc_to = 11_700, 368_000

    def _money(v: float) -> str:
        if v >= 1_000_000:
            s = f"{v/1_000_000:.1f}M"
        elif v >= 1_000:
            s = f"{v/1_000:.1f}K"
        else:
            s = f"{v:.0f}"
        return "$" + s.replace(".0K", "K").replace(".0M", "M")

    x_text = f"{multiple:.1f}X"
    money = f"{_money(mc_from)} —> {_money(mc_to)} 💵"
    text = (
        f"📈 {symbol} is up {x_text} 📈\n"
        f"from ⚡️ Fire Intern Signal\n\n"
        f"{money}\n\n"
        f"💸💸💸💸"
    )
    assert text == (
        "📈 FROGGY is up 31.5X 📈\n"
        "from ⚡️ Fire Intern Signal\n\n"
        "$11.7K —> $368K 💵\n\n"
        "💸💸💸💸"
    )
