"""GMGN OpenAPI client — official API (https://gmgn.ai/ai).

Data-only integration (no trading). Auth for market/token endpoints:
``X-APIKEY`` header + ``timestamp`` + ``client_id`` query params.

Endpoints used:
  GET /v1/token/info                    — price, market cap, volume, holders
  GET /v1/token/security                — renounced, honeypot, rug ratio
  GET /v1/market/token_top_holders      — exact top-10 concentration
  GET /v1/market/rank?chain&interval    — trending tokens (1m granularity)
  POST /v1/trenches                     — new token discovery (pump.fun etc.)

Chain slugs: sol / bsc / base / eth / robinhood (native Robinhood support!)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import aiohttp

from memecoin_alert_bot.utils.helpers import fetch_json, is_valid_api_key

logger = logging.getLogger(__name__)

BASE_URL = "https://openapi.gmgn.ai"

CHAIN_SLUGS = {
    "solana": "sol",
    "robinhood": "robinhood",
}


class GmgnClient:
    """Async client for GMGN's official OpenAPI (data queries only)."""

    def __init__(self, api_key: str = "", session: aiohttp.ClientSession | None = None):
        self.api_key = api_key if is_valid_api_key(api_key) else ""
        self._owned_session = session is None
        self._session_ref = session
        self._session: aiohttp.ClientSession | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = self._session_ref or aiohttp.ClientSession()
            self._session_ref = None
            self._owned_session = True
        return self._session

    async def close(self) -> None:
        if self._owned_session and self._session and not self._session.closed:
            await self._session.close()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _get(self, sub_path: str, query: dict[str, Any]) -> Any:
        """Execute an exist-auth GET with timestamp + client_id query params."""
        if not self.api_key:
            return None
        from urllib.parse import urlencode

        full_query = {k: v for k, v in query.items() if v is not None}
        full_query["timestamp"] = int(time.time())
        full_query["client_id"] = uuid.uuid4().hex
        url = f"{BASE_URL}{sub_path}?{urlencode(full_query)}"
        headers = {"X-APIKEY": self.api_key, "Accept": "application/json"}
        data = await fetch_json(self.session, url, headers=headers, timeout=20)
        if not isinstance(data, dict):
            return None
        if data.get("data") is None and data.get("code") not in (0, "0", None):
            logger.debug("GMGN error on %s: %s", sub_path, data.get("message") or data.get("error"))
            return None
        return data.get("data", data)

    # ── Token endpoints ─────────────────────────────────────────────────

    async def get_token_info(self, chain: str, address: str) -> dict[str, Any] | None:
        return await self._get(
            "/v1/token/info", {"chain": CHAIN_SLUGS.get(chain, chain), "address": address}
        )

    async def get_token_security(self, chain: str, address: str) -> dict[str, Any] | None:
        return await self._get(
            "/v1/token/security", {"chain": CHAIN_SLUGS.get(chain, chain), "address": address}
        )

    async def get_token_top_holders(self, chain: str, address: str, limit: int = 20) -> list[dict[str, Any]]:
        data = await self._get(
            "/v1/market/token_top_holders",
            {"chain": CHAIN_SLUGS.get(chain, chain), "address": address, "limit": min(100, max(10, limit))},
        )
        if isinstance(data, dict):
            return data.get("holders") or data.get("list") or []
        return data if isinstance(data, list) else []

    # ── Market discovery ─────────────────────────────────────────────────

    async def get_trending(
        self,
        chain: str,
        interval: str = "5m",
        limit: int = 50,
        order_by: str = "volume",
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Real-time trending/rank tokens (interval: 1m/5m/1h/6h/24h)."""
        query = {"chain": CHAIN_SLUGS.get(chain, chain), "interval": interval, "limit": min(100, max(1, limit))}
        if filters:
            query.update(filters)
        data = await self._get("/v1/market/rank", query)
        if isinstance(data, dict):
            return data.get("rank") or data.get("list") or data.get("tokens") or []
        return data if isinstance(data, list) else []

    async def get_trenches(
        self,
        chain: str,
        types: list[str] | None = None,
        platforms: list[str] | None = None,
        limit: int = 80,
    ) -> dict[str, list[dict[str, Any]]]:
        """New token discovery (Trenches). Returns keyed category lists."""
        body: dict[str, Any] = {"chain": CHAIN_SLUGS.get(chain, chain), "limit": min(80, max(1, limit))}
        if types:
            body["type"] = types
        if platforms:
            body["launchpad_platform"] = platforms
        headers = {"X-APIKEY": self.api_key, "Content-Type": "application/json"}
        data = await fetch_json(
            self.session,
            f"{BASE_URL}/v1/trenches",
            method="POST",
            headers=headers,
            payload=body,
            timeout=20,
        )
        if isinstance(data, dict) and data.get("data"):
            inner = data["data"]
            return {
                "new_creation": inner.get("new_creation") or [],
                "near_completion": inner.get("pump") or [],
                "completed": inner.get("completed") or [],
            }
        return {"new_creation": [], "near_completion": [], "completed": []}

    # ── Enrichment for CoinData ──────────────────────────────────────────

    async def enrich_coin(self, mint: str, chain: str = "solana") -> dict[str, Any]:
        """Merge token info + security + top holders into CoinData fields.

        Returns keys matching normalizer.merge_enrichment; adds gmgn-specific
        intelligence under sources['gmgn'].
        """
        result: dict[str, Any] = {"sources": {"gmgn": None}}
        if not self.api_key:
            return result

        info, security, holders = await asyncio.gather(
            self.get_token_info(chain, mint),
            self.get_token_security(chain, mint),
            self.get_token_top_holders(chain, mint, limit=20),
            return_exceptions=True,
        )

        intelligence: dict[str, Any] = {}
        if isinstance(info, dict) and info:
            intelligence["info"] = {
                k: info.get(k)
                for k in (
                    "price", "market_cap", "liquidity", "volume", "holder_count",
                    "smart_degen_count", "renowned_wallets", "renowned_count",
                    "bundler_trader_amount_rate", "rat_trader_amount_rate",
                    "sniper_count", "fresh_wallet_rate", "suspected_insider_hold_rate",
                    "top10_holder_rate", "is_on_curve",
                )
                if info.get(k) is not None
            }
            result["market_cap"] = _to_float(info.get("market_cap"))
            result["liquidity"] = _to_float(info.get("liquidity"))
            result["price"] = _to_float(info.get("price"))
            result["holders"] = _to_int(info.get("holder_count"))
            volume = info.get("volume") or info.get("volume_24h")
            if isinstance(volume, dict):
                result["volume_24h"] = _to_float(volume.get("h24") or volume.get("24h") or volume.get("h24_usd"))
            elif volume is not None:
                result["volume_24h"] = _to_float(volume)
            swaps = info.get("swaps") or info.get("swaps_h24") or {}
            if isinstance(swaps, dict):
                result["buys_1h"] = _to_int(swaps.get("h1", {}).get("buys"))
                result["sells_1h"] = _to_int(swaps.get("h1", {}).get("sells"))
                result["buys_5m"] = _to_int(swaps.get("m5", {}).get("buys"))
                result["sells_5m"] = _to_int(swaps.get("m5", {}).get("sells"))
                result["price_change_5m"] = _to_float(swaps.get("m5", {}).get("price_change_percent"))
                result["price_change_1h"] = _to_float(swaps.get("h1", {}).get("price_change_percent"))

        if isinstance(security, dict) and security:
            intelligence["security"] = {
                k: security.get(k)
                for k in ("renounced", "is_honeypot", "rug_ratio", "wash_trading",
                          "open_source", "top10_holder_rate", "sniper_count",
                          "fresh_wallet_rate", "smart_degen_count")
                if security.get(k) is not None
            }
            result.setdefault("safety", {})
            # Honeypot / renounced feed straight into the authority gate.
            if security.get("is_honeypot") is not None:
                result["safety"]["is_honeypot"] = bool(security["is_honeypot"])
            if security.get("renounced") is not None:
                result["safety"]["mint_authority_enabled"] = not bool(security["renounced"])

        if isinstance(intelligence.get("info"), dict) and intelligence["info"].get("top10_holder_rate") is not None:
            pass
        if isinstance(security, dict) and security.get("top10_holder_rate") is not None:
            intelligence.setdefault("security", {})["top10_holder_rate"] = security["top10_holder_rate"]

        parsed_holders = []
        if isinstance(holders, list) and holders:
            for h in holders[:20]:
                pct = _to_float(h.get("percent") or h.get("balance_rate") or h.get("pct"))
                parsed_holders.append(
                    {
                        "address": h.get("wallet_address") or h.get("address") or "",
                        "pct": pct * 100 if pct is not None and pct <= 1.0 else (pct or 0.0),
                        "is_fresh": bool(h.get("is_fresh") or h.get("fresh_wallet")),
                        "is_contract": bool(h.get("is_contract")),
                    }
                )
            result["safety"] = result.get("safety") or {}
            result["safety"]["top_holders"] = parsed_holders

        # Surface top10 concentration wherever GMGN provides it.
        top10 = None
        if isinstance(security, dict):
            top10 = security.get("top10_holder_rate")
        if top10 is not None and isinstance(result.get("safety"), dict):
            result["safety"]["bundled_pct"] = float(top10) * 100 if float(top10) <= 1.0 else float(top10)

        result["sources"] = {"gmgn": intelligence or None}
        return result


# ── helpers ──────────────────────────────────────────────────────────────


def _to_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None