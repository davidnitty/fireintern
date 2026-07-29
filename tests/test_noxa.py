"""Smoke tests for the Noxa launchpad indexer."""

from unittest.mock import AsyncMock

import pytest

from memecoin_alert_bot.data.noxa import (
    ALL_TOKENS_LENGTH_SELECTOR,
    ALL_TOKENS_SELECTOR,
    LAUNCH_FACTORY,
    NoxaIndexer,
)
from memecoin_alert_bot.data.robinhood import CHAIN_ID, RobinhoodChainClient
from memecoin_alert_bot.engine.models import CoinData


def test_noxa_constants():
    assert len(LAUNCH_FACTORY) == 42
    assert ALL_TOKENS_LENGTH_SELECTOR == "0xdbb80e42"
    assert ALL_TOKENS_SELECTOR == "0x634282af"


@pytest.mark.asyncio
async def test_noxa_indexer_polls_registry():
    client = RobinhoodChainClient(session=AsyncMock())
    coins = []

    async def handler(coin: CoinData):
        coins.append(coin)

    indexer = NoxaIndexer(client, token_handler=handler)

    # Simulate registry growing from 3 to 5 with two new tokens.
    call_responses = {
        ALL_TOKENS_LENGTH_SELECTOR: "0x0000000000000000000000000000000000000000000000000000000000000005",
        # index 3
        ALL_TOKENS_SELECTOR + "0000000000000000000000000000000000000000000000000000000000000003": "0x000000000000000000000000aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa3",
        # index 4
        ALL_TOKENS_SELECTOR + "0000000000000000000000000000000000000000000000000000000000000004": "0x000000000000000000000000aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa4",
    }

    async def fake_call(method: str, params: list) -> str:
        data = params[0]["data"]
        return call_responses.get(data)

    client._rpc = AsyncMock(side_effect=fake_call)
    client.fetch_token_metadata = AsyncMock(
        return_value={
            "name": "Noxa Token",
            "symbol": "NOXA",
            "decimals": 18,
            "total_supply": 1_000_000_000,
            "description": "Test",
            "pool_address": "0x0000000000000000000000000000000000000001",
            "socials": {},
        }
    )
    client.fetch_pool_price = AsyncMock(return_value={"price": 1e-9})

    indexer._last_count = 3
    count = await indexer.poll_once()

    assert count == 5
    assert len(coins) == 2
    assert coins[0].chain == "robinhood"
    assert coins[0].chain_id == CHAIN_ID
    assert coins[0].symbol == "NOXA"
