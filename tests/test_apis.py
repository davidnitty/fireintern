"""Smoke tests for Bubblemaps and Bitquery clients."""

from unittest.mock import AsyncMock

import pytest

from memecoin_alert_bot.data.bitquery import BitqueryClient
from memecoin_alert_bot.data.bubblemaps import BubblemapsClient


def test_bubblemaps_no_key_skips():
    client = BubblemapsClient(api_key="", session=AsyncMock())
    assert client.api_key == ""


@pytest.mark.asyncio
async def test_bubblemaps_fallback_returns_none():
    client = BubblemapsClient(api_key="", session=AsyncMock())
    client.fetch_token_map = AsyncMock()
    result = await client.enrich_coin("FAKE_MINT")
    assert result["sources"]["bubblemaps"] is None


def test_bubblemaps_cluster_detection():
    """Verify bundled supply detection from mocked cluster data."""
    client = BubblemapsClient(api_key="fake", session=AsyncMock())

    mock_data = {
        "nodes": [
            {"id": "addr1", "pct": 30.0},
            {"id": "addr2", "pct": 20.0},
            {"id": "addr3", "pct": 15.0},
            {"id": "addr4", "pct": 5.0},
            {"id": "addr5", "pct": 3.0},
        ],
        "clusters": {
            "c1": ["addr1", "addr2", "addr3"],
        },
    }
    result = client._compute_bundling(mock_data)
    safety = result["safety"]

    assert safety["bundled_pct"] == 65.0  # 30+20+15
    assert safety["is_bundled"] is True
    assert len(safety["top_holders"]) == 5
    # top 3 should all be in same cluster → flagged as bundled
    assert safety["top_holders"][0]["is_bundled"]


def test_bitquery_skips_without_key():
    client = BitqueryClient(api_key="", session=AsyncMock())
    assert client.api_key == ""


@pytest.mark.asyncio
async def test_bitquery_fallback_returns_empty():
    client = BitqueryClient(api_key="", session=AsyncMock())
    result = await client.enrich_coin("FAKE_MINT")
    assert result["volume_24h"] is None
    assert result["buy_pressure"] is None
