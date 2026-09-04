"""Tests for the GMGN OpenAPI client and deep link."""

from unittest.mock import AsyncMock

import pytest

from memecoin_alert_bot.data.gmgn import GmgnClient
from memecoin_alert_bot.engine.models import Alert, CoinData


def test_gmgn_keyless_is_disabled():
    client = GmgnClient(api_key="", session=AsyncMock())
    assert client.enabled is False


def test_gmgn_placeholder_key_is_scrubbed():
    client = GmgnClient(api_key="your_gmgn_key_here", session=AsyncMock())
    assert client.enabled is False


@pytest.mark.asyncio
async def test_gmgn_enrichment_maps_fields():
    client = GmgnClient(api_key="real-key-123", session=AsyncMock())
    client.get_token_info = AsyncMock(
        return_value={
            "address": "Mint1",
            "price": 0.00001,
            "market_cap": 25000,
            "liquidity": 9000,
            "holder_count": 310,
            "volume": {"h24": 61000},
            "swaps": {
                "m5": {"buys": 12, "sells": 4, "price_change_percent": 2.5},
                "h1": {"buys": 120, "sells": 80, "price_change_percent": 9.0},
            },
        }
    )
    client.get_token_security = AsyncMock(
        return_value={"renounced": True, "is_honeypot": False, "rug_ratio": 0.1, "top10_holder_rate": 0.28}
    )
    client.get_token_top_holders = AsyncMock(
        return_value=[{"wallet_address": "W1", "percent": 0.042}]
    )

    result = await client.enrich_coin("Mint1", "solana")
    assert result["market_cap"] == 25000
    assert result["price"] == 0.00001
    assert result["holders"] == 310
    assert result["safety"]["mint_authority_enabled"] is False  # renounced
    assert result["safety"]["top_holders"][0]["pct"] == 4.2  # 0.042 -> 4.2%
    assert result["sources"]["gmgn"]["security"]["top10_holder_rate"] == 0.28
    await client.close()


def test_gmgn_url_carries_ca():
    """GMGN button must open the token page with the CA."""
    from memecoin_alert_bot.bot.formatter import _gmgn_url

    coin = CoinData(
        mint="EEpng77ZPn9FbgbT4xsRjwuxNCcMBYq3HTwEscyTpump",
        chain="solana",
        symbol="HeeHaw",
    )
    url = _gmgn_url(coin)
    assert url == f"https://gmgn.ai/sol/token/{coin.mint}"
    assert coin.mint in url


def test_gmgn_url_robinhood_supported():
    """Robinhood cards get a GMGN button too (native chain support)."""
    from memecoin_alert_bot.bot.formatter import _gmgn_url

    coin = CoinData(mint="0x5d9144d2d017386519a7134Fcc7f1E4bA22f920c", chain="robinhood")
    assert _gmgn_url(coin) == f"https://gmgn.ai/robinhood/token/{coin.mint}"


def test_robinhood_card_includes_gmgn_button():
    from memecoin_alert_bot.bot.formatter import format_alert

    coin = CoinData(mint="0xabc", chain="robinhood", symbol="T", name="T", market_cap=50_000)
    _, kb = format_alert(Alert(coin=coin))
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "GMGN" in texts
    assert "Maestro" in texts
