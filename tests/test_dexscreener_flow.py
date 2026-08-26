"""Tests for DexScreener short-window flow normalization."""

from unittest.mock import AsyncMock

import pytest

from memecoin_alert_bot.data.dexscreener import DexScreenerClient


@pytest.mark.asyncio
async def test_dexscreener_maps_windowed_flow_fields():
    client = DexScreenerClient(session=AsyncMock())
    client.fetch_token_pairs = AsyncMock(
        return_value={
            "pairs": [
                {
                    "chainId": "solana",
                    "liquidity": {"usd": 25_000},
                    "volume": {"m5": 500, "h1": 6_000, "h24": 50_000},
                    "txns": {
                        "m5": {"buys": 12, "sells": 4},
                        "h1": {"buys": 100, "sells": 50},
                    },
                    "priceChange": {"m5": 2.5, "h1": 8.0},
                    "priceUsd": "0.0000123",
                    "marketCap": 200_000,
                }
            ]
        }
    )
    result = await client.enrich_coin("Mint", {})
    assert result["volume_5m"] == 500.0
    assert result["volume_1h"] == 6000.0
    assert result["volume_24h"] == 50_000.0
    assert result["buys_5m"] == 12
    assert result["sells_1h"] == 50
    assert result["price_change_5m"] == 2.5
    assert result["flow_data_quality"] == "verified_usd"
    await client.close()
