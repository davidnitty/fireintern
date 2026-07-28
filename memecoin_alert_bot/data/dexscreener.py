"""DexScreener API client for volume, liquidity and market-cap cross-reference."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from memecoin_alert_bot.utils.helpers import fetch_json

logger = logging.getLogger(__name__)

BASE_URL = "https://api.dexscreener.com/latest/dex"


class DexScreenerClient:
    """Thin async wrapper around the DexScreener API."""

    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._owned_session = session is None
        self.session = session or aiohttp.ClientSession()

    async def close(self) -> None:
        if self._owned_session and not self.session.closed:
            await self.session.close()

    async def fetch_token_pairs(self, mint: str) -> dict[str, Any] | None:
        """Fetch all pairs for a Solana token address."""
        url = f"{BASE_URL}/tokens/{mint}"
        return await fetch_json(self.session, url, timeout=15)

    async def get_boosted_tokens(self) -> list[dict[str, Any]]:
        """Fetch currently boosted tokens."""
        data = await fetch_json(self.session, f"{BASE_URL}/boosts/top/v1", timeout=15)
        return data if isinstance(data, list) else []

    async def enrich_coin(self, mint: str, base: dict[str, Any]) -> dict[str, Any]:
        """Return merged metadata keyed by the fields CoinData expects."""
        result = {
            "volume_24h": None,
            "liquidity": None,
            "price": None,
            "market_cap": None,
            "sources": {"dexscreener": None},
        }
        data = await self.fetch_token_pairs(mint)
        result["sources"]["dexscreener"] = data
        pairs = data.get("pairs", []) if data else []
        if not pairs:
            return result

        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        best = max(
            sol_pairs or pairs,
            key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0),
            default=None,
        )
        if best is None:
            return result

        result["volume_24h"] = float(best.get("volume", {}).get("h24", 0) or 0)
        result["liquidity"] = float(best.get("liquidity", {}).get("usd", 0) or 0)
        result["price"] = float(best.get("priceUsd", 0) or 0)
        result["market_cap"] = float(best.get("marketCap", 0) or 0)
        if result["market_cap"] and not result["volume_24h"]:
            result["volume_24h"] = float(best.get("volume", {}).get("h24", 0) or 0)
        result["sources"]["dexscreener"] = best
        return result
