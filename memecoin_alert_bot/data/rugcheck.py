"""Rugcheck.xyz API client for token safety reports."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from memecoin_alert_bot.utils.helpers import fetch_json

logger = logging.getLogger(__name__)

BASE_URL = "https://api.rugcheck.xyz/v1"


class RugcheckClient:
    """Async wrapper around Rugcheck token reports."""

    def __init__(self, api_key: str = "", session: aiohttp.ClientSession | None = None):
        self.api_key = api_key
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._owned_session = session is None
        self.session = session or aiohttp.ClientSession(headers=headers)

    async def close(self) -> None:
        if self._owned_session and not self.session.closed:
            await self.session.close()

    async def fetch_report(self, mint: str) -> dict[str, Any] | None:
        """Fetch Rugcheck report for a token."""
        return await fetch_json(
            self.session, f"{BASE_URL}/tokens/{mint}/report", timeout=20
        )

    async def enrich_coin(self, mint: str, base: dict[str, Any]) -> dict[str, Any]:
        """Return safety flags normalized for CoinData."""
        result = {
            "safety": {
                "lp_locked": None,
                "lp_locked_pct": None,
                "mint_authority_enabled": None,
                "freeze_authority_enabled": None,
                "rugcheck_score": None,
                "is_honeypot": None,
                "bundled_pct": 0.0,
            },
            "sources": {"rugcheck": None},
        }
        data = await self.fetch_report(mint)
        result["sources"]["rugcheck"] = data
        if not data:
            return result

        result["safety"]["rugcheck_score"] = data.get("score")
        result["safety"]["is_honeypot"] = bool(data.get("verification", {}).get("isHoneypot"))
        result["safety"]["mint_authority_enabled"] = bool(
            data.get("token", {}).get("mintAuthority")
        )
        result["safety"]["freeze_authority_enabled"] = bool(
            data.get("token", {}).get("freezeAuthority")
        )

        markets = data.get("markets", [])
        if markets:
            liquidity = markets[0].get("liquidityA", {})
            lp_mint = liquidity.get("lpMint", "")
            # Heuristic: non-empty lpMint suggests LP exists; lock status requires deeper check.
            result["safety"]["lp_locked"] = lp_mint != ""

        holders = data.get("topHolders", [])
        bundled = 0.0
        bundled_wallets = 0
        for h in holders:
            pct = h.get("pct", 0) or 0
            if h.get("isBundled") or h.get("risk") in ("bundled", "sol_bundled"):
                bundled += pct
                bundled_wallets += 1
        result["safety"]["bundled_pct"] = bundled
        result["safety"]["bundled_wallets"] = bundled_wallets
        return result
