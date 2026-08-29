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
    """Regression: DexScreener's chainId is 'robinhood' (NOT 'robinhoodchain')."""
    assert DEXSCREENER_SLUG == "robinhood"
    assert "robinhood" in DEFAULT_RPC


def _pair(mint="0x547C0DB20565905f00e1c0FF6745AAA7D273b27A", liq=77_600, symbol="DOG"):
    import time

    return {
        "pairAddress": "0x" + "a" * 64,  # v4 pool ID (not an address)
        "chainId": "robinhood",
        "baseToken": {"address": mint, "symbol": symbol, "name": f"{symbol} Token"},
        "quoteToken": {"address": "0x" + "b" * 40},
        "liquidity": {"usd": liq},
        "volume": {"m5": 2_800, "h1": 169_000, "h24": 1_310_000},
        "txns": {"m5": {"buys": 7, "sells": 11}, "h1": {"buys": 246, "sells": 253}},
        "priceChange": {"m5": 1.1, "h1": -33},
        "priceUsd": "0.000163",
        "marketCap": 163_000,
        "pairCreatedAt": int(time.time() * 1000) - 10 * 60_000,  # 10 min old
        "info": {"socials": [{"type": "twitter", "url": "https://x.com/dog"}]},
    }


@pytest.mark.asyncio
async def test_direct_discovery_emits_via_profile_feed():
    """A directly-deployed token (not Pons/Noxa) must be discovered."""
    dexscreener = AsyncMock()
    dexscreener.get_latest_profiles = AsyncMock(
        return_value=[
            {"chainId": "robinhood", "tokenAddress": "0x547C0DB20565905f00e1c0FF6745AAA7D273b27A"},
            {"chainId": "solana", "tokenAddress": "SolMint111111111"},  # filtered out
        ]
    )
    dexscreener.fetch_token_pairs = AsyncMock(
        return_value={"pairs": [_pair()]}
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
        dexscreener, robinhood, token_handler=coins.append
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
    dexscreener.get_latest_profiles = AsyncMock(
        return_value=[{"chainId": "robinhood", "tokenAddress": "0x" + "d" * 40}]
    )
    dexscreener.fetch_token_pairs = AsyncMock(
        return_value={"pairs": [_pair(mint="0x" + "d" * 40, liq=50, symbol="DUST")]}
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
    dexscreener = AsyncMock()
    dexscreener.get_latest_profiles = AsyncMock(
        return_value=[{"chainId": "robinhood", "tokenAddress": "0x" + "f" * 40}]
    )
    dexscreener.fetch_token_pairs = AsyncMock(
        return_value={"pairs": [_pair(mint="0x" + "f" * 40, symbol="REPEAT", liq=20_000)]}
    )
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


@pytest.mark.asyncio
async def test_direct_discovery_ignores_other_chains_in_pairs():
    """Pairs on other chains must not be emitted for a robinhood profile."""
    dexscreener = AsyncMock()
    dexscreener.get_latest_profiles = AsyncMock(
        return_value=[{"chainId": "robinhood", "tokenAddress": "0x" + "9" * 40}]
    )
    other = _pair(mint="0x" + "9" * 40, liq=500_000, symbol="OTHER")
    other["chainId"] = "solana"
    dexscreener.fetch_token_pairs = AsyncMock(return_value={"pairs": [other]})
    robinhood = AsyncMock()
    coins: list[CoinData] = []
    indexer = DirectDiscoveryIndexer(
        dexscreener, robinhood, token_handler=coins.append
    )
    emitted = await indexer.poll_once()
    assert emitted == 0
