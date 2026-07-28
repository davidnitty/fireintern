"""Solscan Pro API client for holder and metadata lookup."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from memecoin_alert_bot.utils.helpers import fetch_json

logger = logging.getLogger(__name__)

BASE_URL = "https://pro-api.solscan.io/v2.0"


class SolscanClient:
    """Async wrapper for Solscan token and holder endpoints."""

    def __init__(self, api_key: str, session: aiohttp.ClientSession | None = None):
        self.api_key = api_key
        self._owned_session = session is None
        self.session = session or aiohttp.ClientSession(
            headers={"token": api_key, "Accept": "application/json"}
        )

    async def close(self) -> None:
        if self._owned_session and not self.session.closed:
            await self.session.close()

    async def fetch_token_meta(self, mint: str) -> dict[str, Any] | None:
        """Fetch token metadata."""
        return await fetch_json(
            self.session, f"{BASE_URL}/token/meta?tokenAddress={mint}", timeout=15
        )

    async def fetch_token_holders(self, mint: str, limit: int = 20) -> list[dict[str, Any]]:
        """Fetch top token holders."""
        result = []
        for offset in range(0, limit, 10):
            data = await fetch_json(
                self.session,
                f"{BASE_URL}/token/holders?tokenAddress={mint}&offset={offset}&limit=10",
                timeout=15,
            )
            items = data.get("data", {}).get("items", []) if data else []
            if not items:
                break
            result.extend(items)
            if len(items) < 10:
                break
        return result

    async def enrich_coin(self, mint: str, base: dict[str, Any]) -> dict[str, Any]:
        """Return safety/holder metadata normalized for CoinData."""
        result = {
            "holders": None,
            "safety": {"top_holders": []},
            "sources": {"solscan": None},
        }
        if not self.api_key:
            return result

        meta = await self.fetch_token_meta(mint)
        result["sources"]["solscan"] = meta
        if meta and meta.get("success"):
            data = meta.get("data", {})
            result["holders"] = int(data.get("holder", 0) or 0)

        holders = await self.fetch_token_holders(mint)
        top = []
        for h in holders:
            pct = 0.0
            if "percentage" in h:
                pct = float(h["percentage"])
            elif "amount" in h:
                total_supply = float(base.get("total_supply", 1) or 1)
                if total_supply > 0:
                    pct = float(h["amount"]) / total_supply * 100
            top.append(
                {
                    "address": h.get("address", ""),
                    "pct": pct,
                    "is_fresh": False,
                    "is_contract": False,
                }
            )
        result["safety"]["top_holders"] = top
        return result
