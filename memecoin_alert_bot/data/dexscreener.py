"""DexScreener API client for volume, liquidity and market-cap cross-reference."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from memecoin_alert_bot.utils.helpers import fetch_json

logger = logging.getLogger(__name__)

BASE_URL = "https://api.dexscreener.com/latest/dex"


def import_time() -> float:
    import time

    return time.time()


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

    async def get_new_pairs(self, chain_slug: str, max_age_minutes: int = 30) -> list[dict[str, Any]]:
        """Discover recently created pairs on a chain via DexScreener search.

        Uses the public search endpoint sorted by pair age; returns raw pair
        objects for the caller to filter (liquidity floor, quote token, etc.).
        """
        url = f"{BASE_URL}/search?q={chain_slug}"
        data = await fetch_json(self.session, url, timeout=20)
        pairs = data.get("pairs", []) if data else []
        now_ms = import_time() * 1000
        fresh = []
        for pair in pairs:
            if pair.get("chainId") != chain_slug:
                continue
            created = pair.get("pairCreatedAt")
            if not created:
                continue
            age_minutes = (now_ms - int(created)) / 60_000
            if 0 <= age_minutes <= max_age_minutes:
                fresh.append(pair)
        return fresh

    async def enrich_coin(self, mint: str, base: dict[str, Any], chain: str = "solana") -> dict[str, Any]:
        """Return merged metadata keyed by the fields CoinData expects."""
        chain_slug = {"robinhood": "robinhoodchain"}.get(chain, chain)
        result = {
            "volume_24h": None,
            "volume_5m": None,
            "volume_1h": None,
            "buys_5m": None,
            "sells_5m": None,
            "buys_1h": None,
            "sells_1h": None,
            "price_change_5m": None,
            "price_change_1h": None,
            "flow_data_quality": "unknown",
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

        chain_pairs = [p for p in pairs if p.get("chainId") == chain_slug]
        best = max(
            chain_pairs or pairs,
            key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0),
            default=None,
        )
        if best is None:
            return result

        volume = best.get("volume", {})
        txns = best.get("txns", {})
        price_change = best.get("priceChange", {})

        # DexScreener values are USD-normalized for the selected pair.
        result["volume_24h"] = float(volume.get("h24", 0) or 0)
        result["volume_5m"] = float(volume.get("m5", 0) or 0)
        result["volume_1h"] = float(volume.get("h1", 0) or 0)
        result["buys_5m"] = int(txns.get("m5", {}).get("buys", 0) or 0)
        result["sells_5m"] = int(txns.get("m5", {}).get("sells", 0) or 0)
        result["buys_1h"] = int(txns.get("h1", {}).get("buys", 0) or 0)
        result["sells_1h"] = int(txns.get("h1", {}).get("sells", 0) or 0)
        result["price_change_5m"] = float(price_change.get("m5", 0) or 0)
        result["price_change_1h"] = float(price_change.get("h1", 0) or 0)
        result["liquidity"] = float(best.get("liquidity", {}).get("usd", 0) or 0)
        result["price"] = float(best.get("priceUsd", 0) or 0)
        result["market_cap"] = float(best.get("marketCap", 0) or 0)
        result["flow_data_quality"] = "verified_usd"
        result["sources"]["dexscreener"] = best
        return result
