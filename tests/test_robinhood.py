"""Smoke tests for Robinhood Chain and Pons integration."""

from unittest.mock import AsyncMock

import pytest

from eth_utils import to_checksum_address

from memecoin_alert_bot.data.pons import PonsIndexer
from memecoin_alert_bot.data.robinhood import CHAIN_ID, DEFAULT_RPC, RobinhoodChainClient
from memecoin_alert_bot.engine.models import CoinData


def test_robinhood_client_constants():
    assert CHAIN_ID == 4663
    assert "robinhood" in DEFAULT_RPC


def test_function_selectors_consistent():
    """Ensure our hand-derived selectors match web3 keccak output."""
    client = RobinhoodChainClient()
    assert client.selector("name") == "06fdde03"
    assert client.selector("symbol") == "95d89b41"
    assert client.selector("decimals") == "313ce567"


@pytest.mark.asyncio
async def test_decode_string_returns_empty_on_bad_input():
    client = RobinhoodChainClient()
    assert client.decode_string("") == ""
    assert client.decode_string("0x") == ""
    assert client.decode_string("nothex") == ""


def test_sqrt_price_x96_conversion():
    client = RobinhoodChainClient()
    assert client.decode_sqrt_price_x96(0) == 0.0
    # sqrt(1)*2^96 = 2^96 => price = 1
    assert abs(client.decode_sqrt_price_x96(2**96) - 1.0) < 1e-9


def test_pons_indexer_processes_mock_log():
    client = RobinhoodChainClient()
    coins_received = []

    async def handler(coin: CoinData):
        coins_received.append(coin)

    indexer = PonsIndexer(client, token_handler=handler)

    token = to_checksum_address("0x1111111111111111111111111111111111111111")
    deployer = to_checksum_address("0x2222222222222222222222222222222222222222")

    # Minimal TokenLaunched log shape
    log = {
        "address": "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB",
        "topics": [
            "0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a",
            "0x" + "0" * 24 + token[2:],
            "0x" + "0" * 24 + deployer[2:],
        ],
        "data": "0x" + "0" * 64 * 7,  # dummy bytes for non-indexed params
        "blockNumber": "0x1234",
        "transactionHash": "0xabcd",
    }

    # Patch out downstream RPC calls
    indexer.client.fetch_token_metadata = AsyncMock(
        return_value={
            "name": "Test Token",
            "symbol": "TEST",
            "decimals": 18,
            "total_supply": 1_000_000,
            "description": "A test token",
            "pool_address": "",
            "socials": {"twitter": "https://x.com/testcoin"},
        }
    )
    indexer.client.fetch_pool_price = AsyncMock(return_value={"price": None})

    import asyncio

    coin = asyncio.run(indexer._process_log(log))

    assert coin is not None
    assert coin.chain == "robinhood"
    assert coin.symbol == "TEST"
    assert coin.deployer == deployer.lower()


def test_owner_selector_registered():
    """Regression: missing 'owner' selector crashed Noxa/Pons metadata fetch."""
    from memecoin_alert_bot.data.robinhood import FUNCTION_SELECTORS
    assert "owner" in FUNCTION_SELECTORS
    assert FUNCTION_SELECTORS["owner"] == "8da5cb5b"
