"""Tests for direct ERC-20 discovery on Robinhood Chain."""

from unittest.mock import AsyncMock

import pytest

from memecoin_alert_bot.data.direct_discovery import (
    DEXSCREENER_SLUG,
    DirectDiscoveryIndexer,
)
from memecoin_alert_bot.data.robinhood import DEFAULT_RPC
from memecoin_alert_bot.engine.models import CoinData


def test_slug_matches_dexscreener_chain_id():
    assert DEXSCREENER_SLUG == "robinhoodchain"
    assert "robinhood" in DEFAULT_RPC


@pytest.mark.asyncio
async def test_direct_discovery_emits_direct_deployments():
    """A directly-deployed token (not Pons/Noxa) must be discovered."""
    dexscreener = AsyncMock()
    dexscreener.get_new_pairs = AsyncMock(
        return_value=[
            {
                "pairAddress": "0x" + "a" * 64,  # v4 pool ID (not an address)
                "baseToken": {"address": "0x547C0DB20565905f00e1c0FF6745AAA7D273b27A", "symbol": "DOG", "name": "DoggyStyle"},
                "quoteToken": {"address": "0x" + "b" * 40},
                "liquidity": {"usd": 77_600},
                "volume": {"m5": 2_800, "h1": 169_000, "h24": 1_310_000},
                "txns": {"m5": {"buys": 7, "sells": 11}, "h1": {"buys": 246, "sells": 253}},
                "priceChange": {"m5": 1.1, "h1": -33},
                "priceUsd": "0.000163",
                "marketCap": 163_000,
                "pairCreatedAt": 1_790_000_000_000,
                "info": {"socials": [{"type": "twitter", "url": "https://x.com/dog"}]},
            }
        ]
    )
    robinhood = AsyncMock()
    robinhood.fetch_token_metadata = AsyncMock(
        return_value={
            "name": "DoggyStyle",
            "symbol": "DOGGYSTYLE",
            "decimals": 18,
            "total_supply": 1_000_000_000,
            "description": "",
            "pool_address": "",
            "owner": "",
            "ownership_renounced": True,
            "socials": {},
        }
    )

    coins: list[CoinData] = []
    indexer = DirectDiscoveryIndexer(
        dexscreener, robinhood, token_handler=coins.append, max_age_minutes=30
    )
    emitted = await indexer.poll_once()

    assert emitted == 1
    coin = coins[0]
    assert coin.mint == "0x547C0DB20565905f00e1c0FF6745AAA7D273b27A"
    assert coin.chain == "robinhood"
    assert coin.market_cap == 163_000
    assert coin.volume_24h == 1_310_000
    assert coin.flow_data_quality == "verified_usd"
    assert coin.social_links.get("twitter") == "https://x.com/dog"
    # v4 pool ID is not an EVM address; must not be stored as pool_address.
    assert coin.pool_address is None


@pytest.mark.asyncio
async def test_direct_discovery_skips_dust_pools():
    dexscreener = AsyncMock()
    dexscreener.get_new_pairs = AsyncMock(
        return_value=[
            {
                "pairAddress": "0x" + "c" * 40,
                "baseToken": {"address": "0x" + "d" * 40, "symbol": "DUST"},
                "liquidity": {"usd": 50},
            }
        ]
    )
    robinhood = AsyncMock()
    coins: list[CoinData] = []
    indexer = DirectDiscoveryIndexer(
        dexscreener, robinhood, token_handler=coins.append
    )
    emitted = await indexer.poll_once()
    assert emitted == 0
    assert coins == []


@pytest.mark.asyncio
async def test_direct_discovery_dedupes_seen_mints():
    pair = {
        "pairAddress": "0x" + "e" * 40,
        "baseToken": {"address": "0x" + "f" * 40, "symbol": "REPEAT"},
        "liquidity": {"usd": 20_000},
    }
    dexscreener = AsyncMock()
    dexscreener.get_new_pairs = AsyncMock(return_value=[pair])
    robinhood = AsyncMock()
    robinhood.fetch_token_metadata = AsyncMock(
        return_value={"name": "Repeat", "symbol": "REPEAT", "socials": {}}
    )
    coins: list[CoinData] = []
    indexer = DirectDiscoveryIndexer(
        dexscreener, robinhood, token_handler=coins.append
    )
    await indexer.poll_once()
    await indexer.poll_once()
    assert len(coins) == 1
