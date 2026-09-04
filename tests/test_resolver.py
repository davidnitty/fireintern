"""Tests for the DNS-cache + DoH fallback resolver."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from memecoin_alert_bot.utils.resolver import CachedDohResolver, _is_ip


def test_is_ip():
    assert _is_ip("1.2.3.4") is True
    assert _is_ip("api.dexscreener.com") is False
    assert _is_ip("999.1.1.1") is False


@pytest.mark.asyncio
async def test_system_dns_cached():
    resolver = CachedDohResolver()
    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.getaddrinfo = AsyncMock(
            return_value=[(None, None, None, "", ("1.2.3.4", 443))]
        )
        result = await resolver.resolve("api.example.com", 443)
        assert result[0]["host"] == "1.2.3.4"

        # Second call served from cache (system resolver NOT called again).
        mock_loop.return_value.getaddrinfo = AsyncMock(side_effect=OSError("dns down"))
        result = await resolver.resolve("api.example.com", 443)
        assert result[0]["host"] == "1.2.3.4"


@pytest.mark.asyncio
async def test_doh_fallback_on_system_failure():
    resolver = CachedDohResolver()

    async def fake_doh(endpoint, host):
        return ["5.6.7.8"]

    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.getaddrinfo = AsyncMock(side_effect=OSError("getaddrinfo failed"))
        resolver._doh_resolve = fake_doh
        result = await resolver.resolve("api.dexscreener.com", 443)
        assert result[0]["host"] == "5.6.7.8"


@pytest.mark.asyncio
async def test_stale_cache_served_during_total_outage():
    resolver = CachedDohResolver()
    # Seed an expired cache entry.
    import time

    import socket as _socket
    resolver._cache[f"old.example.com|{_socket.AF_INET}"] = (["9.9.9.9"], time.time() - 120)

    async def fail_doh(endpoint, host):
        return []

    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.getaddrinfo = AsyncMock(side_effect=OSError("down"))
        resolver._doh_resolve = fail_doh
        result = await resolver.resolve("old.example.com", 443)
        assert result[0]["host"] == "9.9.9.9"  # stale but usable


def test_event_loop_smoke():
    async def run():
        resolver = CachedDohResolver()
        await resolver.close()

    asyncio.run(run())
