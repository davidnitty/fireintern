"""pump.fun REST API client (unofficial endpoints, use with caution)."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from memecoin_alert_bot.utils.helpers import fetch_json

logger = logging.getLogger(__name__)

BASE_URL = "https://frontend-api.pump.fun"


class PumpFunClient:
    """Fetch pump.fun coin metadata and recent trades."""

    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._owned_session = session is None
        self.session = session or aiohttp.ClientSession(
            headers={
                "Accept": "application/json",
                "Referer": "https://pump.fun/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
            }
        )

    async def close(self) -> None:
        if self._owned_session and not self.session.closed:
            await self.session.close()

    async def fetch_coin(self, mint: str) -> dict[str, Any] | None:
        """Fetch coin details from pump.fun API."""
        url = f"{BASE_URL}/coins/{mint}"
        return await fetch_json(self.session, url, timeout=15)

    async def fetch_trades(self, mint: str, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch recent trades for a coin."""
        url = f"{BASE_URL}/trades/{mint}?limit={limit}&offset=0"
        data = await fetch_json(self.session, url, timeout=15)
        return data if isinstance(data, list) else []

    async def enrich_coin(self, mint: str, base: dict[str, Any]) -> dict[str, Any]:
        """Return normalized metadata from pump.fun."""
        result = {
            "symbol": base.get("symbol", "UNKNOWN"),
            "name": base.get("name", ""),
            "description": base.get("description", ""),
            "market_cap": None,
            "volume_24h": None,
            "liquidity": None,
            "price": None,
            "holders": None,
            "age_seconds": None,
            "dev_wallet": "",
            "social_links": {},
            "sources": {"pumpfun": None},
            "tokenized_agent": False,
        }
        data = await self.fetch_coin(mint)
        if not data:
            return result

        result["sources"]["pumpfun"] = data
        result["symbol"] = data.get("symbol", result["symbol"])
        result["name"] = data.get("name", result["name"])
        result["description"] = data.get("description", result["description"])
        result["dev_wallet"] = data.get("creator", "")
        result["market_cap"] = float(data.get("market_cap", 0) or 0)
        result["liquidity"] = float(data.get("liquidity", 0) or 0)
        result["price"] = float(data.get("price", 0) or 0)
        result["holders"] = int(data.get("holder_count", 0) or 0)
        result["volume_24h"] = float(data.get("volume_24h", 0) or 0)

        created = data.get("created_timestamp")
        if created:
            import time

            result["age_seconds"] = int(time.time()) - int(created / 1000)

        result["social_links"] = {
            k: v
            for k, v in {
                "twitter": data.get("twitter"),
                "telegram": data.get("telegram"),
                "website": data.get("website"),
            }.items()
            if v
        }
        result["tokenized_agent"] = bool(data.get("is_tokenized_agent"))
        return result
