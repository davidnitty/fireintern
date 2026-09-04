"""DNS resilience: cached resolver with DNS-over-HTTPS fallback.

The bot's host machine has intermittent DNS failures ("getaddrinfo
failed" across all API hosts). This resolver:
  1. serves hosts from a short TTL cache,
  2. falls back to the system resolver,
  3. on system-DNS failure, resolves via Cloudflare / Google DoH
     (queried by IP literal — no DNS needed),
  4. on total failure, serves stale cache entries rather than raising.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DOH_ENDPOINTS = [
    "https://1.1.1.1/dns-query?name={host}&type=A",
    "https://dns.google/resolve?name={host}&type=A",
]
CACHE_TTL_DEFAULT = 300  # seconds
CACHE_TTL_MIN = 60
CACHE_TTL_MAX = 600
STALE_GRACE = 3600  # serve stale entries up to 1h old during outages


class CachedDohResolver(aiohttp.abc.AbstractResolver):
    """aiohttp resolver: system DNS -> DoH fallback -> stale cache."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[list[str], float]] = {}

    async def close(self) -> None:  # pragma: no cover - nothing to release
        return None

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
        traces: Any = None,
    ) -> list[dict[str, Any]]:
        # IPv6 requests bypass the cache/DoH path (DoH feed is A-record only).
        if family not in (0, socket.AF_INET) and not _is_ip(host):
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(host, port, family=family)
            return [
                {
                    "hostname": "",
                    "host": info[4][0],
                    "port": port,
                    "family": info[0],
                    "proto": 0,
                    "flags": 0,
                }
                for info in infos
            ]

        if _is_ip(host):
            return [_entry(host, port, family)]

        cache_key = f"{host}|{family}"
        cached = self._cache.get(cache_key)
        if cached and cached[1] > time.time():
            return [_entry(ip, port, family) for ip in cached[0]]

        # 1) system resolver
        try:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(host, port, family=socket.AF_INET)
            ips = [info[4][0] for info in infos]
            self._store(cache_key, ips, CACHE_TTL_DEFAULT)
            return [_entry(ip, port, family) for ip in ips]
        except Exception as exc:
            logger.debug("System DNS failed for %s: %s", host, exc)

        # 2) DoH fallbacks
        for endpoint in DOH_ENDPOINTS:
            try:
                ips = await self._doh_resolve(endpoint, host)
                if ips:
                    self._store(cache_key, ips, CACHE_TTL_DEFAULT)
                    logger.info("DoH fallback resolved %s -> %s", host, ips[0])
                    return [_entry(ip, port, family) for ip in ips]
            except Exception as exc:
                logger.debug("DoH %s failed for %s: %s", endpoint, host, exc)

        # 3) stale cache during prolonged outages
        if cached and (time.time() - cached[1]) < STALE_GRACE:
            logger.warning("Serving STALE DNS for %s (%s)", host, cached[0][0])
            return [_entry(ip, port, family) for ip in cached[0]]

        raise OSError(f"Cannot resolve {host}")

    async def _doh_resolve(self, endpoint: str, host: str) -> list[str]:
        url = endpoint.format(host=host)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"accept": "application/dns-json"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)

        answers = data.get("Answer") or []
        ips = [a["data"] for a in answers if a.get("type") == 1 and a.get("data")]
        return ips

    def _store(self, cache_key: str, ips: list[str], ttl: int) -> None:
        clamped = max(CACHE_TTL_MIN, min(CACHE_TTL_MAX, ttl))
        self._cache[cache_key] = (ips, time.time() + clamped)


def _is_ip(host: str) -> bool:
    parts = host.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def _entry(ip: str, port: int, family: int = socket.AF_INET) -> dict[str, Any]:
    return {
        "hostname": "",
        "host": ip,
        "port": port,
        "family": family,
        "proto": 0,
        "flags": 0,
    }