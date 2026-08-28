"""Tests for metadata-URI identity rescue (no more UNKNOWN names)."""

import asyncio
from types import SimpleNamespace

from memecoin_alert_bot.engine.normalizer import create_from_pumpportal
from memecoin_alert_bot.utils.helpers import fetch_metadata_json


def test_create_event_captures_metadata_uri():
    event = {
        "mint": "MetaMint11111111111111111111111111111111111",
        "symbol": "UNKNOWN",
        "name": "",
        "uri": "ipfs://QmXyz/meta.json",
        "marketCapSol": 30,
    }
    coin = create_from_pumpportal(event, sol_usd=170.0)
    assert coin.metadata_uri == "ipfs://QmXyz/meta.json"
    assert coin.symbol == "UNKNOWN"  # unchanged until rescue runs


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload
        self.last_url = None

    def get(self, url, **kwargs):
        self.last_url = url
        return _FakeResponse(self._payload)


def test_fetch_metadata_json_rewrites_ipfs_and_parses():
    session = _FakeSession({"name": "Punch Token", "symbol": "PUNCH", "description": "d"})

    async def run():
        return await fetch_metadata_json(session, "ipfs://QmXyz/meta.json")

    meta = asyncio.run(run())
    assert session.last_url == "https://ipfs.io/ipfs/QmXyz/meta.json"
    assert meta == {"name": "Punch Token", "symbol": "PUNCH", "description": "d"}


def test_fetch_metadata_json_none_on_bad_payload():
    session = _FakeSession("not a dict")

    async def run():
        return await fetch_metadata_json(session, "https://example.com/meta.json")

    assert asyncio.run(run()) is None


def test_fetch_metadata_json_none_for_empty_uri():
    assert asyncio.run(fetch_metadata_json(_FakeSession({}), "")) is None


def test_identity_rescue_flow():
    """Simulate: event had no name; URI fills it before evaluate sees UNKNOWN."""
    event = {
        "mint": "MetaMint22222222222222222222222222222222222",
        "symbol": "UNKNOWN",
        "name": "",
        "uri": "ipfs://QmAbc/meta.json",
    }
    coin = create_from_pumpportal(event, sol_usd=170.0)

    session = _FakeSession({"name": "Real Name", "symbol": "REAL", "description": ""})

    async def rescue():
        meta = await fetch_metadata_json(session, coin.metadata_uri)
        if meta:
            if meta["name"] and not coin.name:
                coin.name = meta["name"]
            if meta["symbol"] and coin.symbol == "UNKNOWN":
                coin.symbol = meta["symbol"]

    asyncio.run(rescue())
    assert coin.name == "Real Name"
    assert coin.symbol == "REAL"
