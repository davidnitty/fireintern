"""Tests for the StockYard stock-pair discovery client."""

from unittest.mock import AsyncMock

import pytest

from memecoin_alert_bot.data.stockyard import StockyardClient


@pytest.mark.asyncio
async def test_stockyard_parses_paired_memecoins():
    client = StockyardClient(session=AsyncMock())
    client.get_map = AsyncMock(
        return_value={
            "asOf": "2026-09-04 19:07 UTC",
            "rows": [
                {
                    "t": "COST",
                    "name": "Costco • Robinhood Token",
                    "a": "0x4EA005168D7F09a7A0Ba9D1DEf21a479950E44C2",
                    "p": "914.34",
                    "m": [
                        {
                            "s": "HOLO",
                            "a": "0xe48E3ef04a915595A79a3d2C8b17A02325321E18",
                            "l": 22390148,
                            "v": 1654,
                            "tx": 16,
                            "mc": 22390133,
                            "age": 0.5,
                            "u": "https://dexscreener.com/robinhood/0x8cb1e78007e4c0d9e502c9281916fb77534e63ddec8b8ed7e8960f58968e1459",
                            "lp": "Long",
                        },
                        {"s": "BAD", "a": "", "mc": 1},
                    ],
                }
            ],
        }
    )
    memes = await client.get_paired_memecoins()
    assert len(memes) == 1  # entries without an address are dropped
    m = memes[0]
    assert m["mint"] == "0xe48E3ef04a915595A79a3d2C8b17A02325321E18"
    assert m["symbol"] == "HOLO"
    assert m["market_cap"] == 22390133
    assert m["volume_24h"] == 1654
    assert m["stock_ticker"] == "COST"
    assert m["launchpad"] == "Long"
    # v4 pool id extracted from the dexscreener link (64-hex tail)
    assert len(m["pool_id"]) == 64
    await client.close()


@pytest.mark.asyncio
async def test_stockyard_handles_bad_payload():
    client = StockyardClient(session=AsyncMock())
    client.get_map = AsyncMock(return_value={"unexpected": True})
    assert await client.get_paired_memecoins() == []
    client.get_map = AsyncMock(return_value=None)
    assert await client.get_paired_memecoins() == []
    await client.close()