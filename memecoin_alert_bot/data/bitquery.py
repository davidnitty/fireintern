"""Bitquery GraphQL API client — stable primary data source for Solana tokens.

Endpoint: POST https://graphql.bitquery.io/
Auth:     X-API-KEY header
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from memecoin_alert_bot.utils.helpers import fetch_json

logger = logging.getLogger(__name__)

BITQUERY_URL = "https://graphql.bitquery.io"


class BitqueryClient:
    """Async GraphQL client for Bitquery Solana data."""

    def __init__(
        self,
        api_key: str = "",
        session: aiohttp.ClientSession | None = None,
    ):
        self.api_key = api_key
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if api_key:
            headers["X-API-KEY"] = api_key
        self._owned_session = session is None
        self.session = session or aiohttp.ClientSession(headers=headers)

    async def close(self) -> None:
        if self._owned_session and not self.session.closed:
            await self.session.close()

    async def _query(self, query: str, variables: dict = None) -> dict[str, Any] | None:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        return await fetch_json(
            self.session,
            BITQUERY_URL,
            method="POST",
            payload=payload,
            timeout=25,
        )

    async def fetch_token_stats(self, mint: str) -> dict[str, Any] | None:
        """Fetch 24h volume, price, and trade stats for a Solana token."""
        query = """
        query($mint: String!) {
          Solana {
            DEXTradeByTokens(
              where: {Trade: {Currency: {MintAddress: {is: $mint}}}}
              orderBy: {descending: Block_Time}
              limit: {count: 100}
            ) {
              Trade {
                Buy {
                  Amount
                  Price
                  Currency { Symbol Name MintAddress }
                }
                Sell {
                  Amount
                  Currency { Symbol }
                }
              }
              Block { Time }
              Transaction { Signature }
            }
          }
        }
        """
        data = await self._query(query, {"mint": mint})
        return data

    async def enrich_coin(self, mint: str) -> dict[str, Any]:
        """Return volume, holders, and buy/sell data from Bitquery.

        Falls back silently when no API key is configured.
        """
        result: dict[str, Any] = {
            "volume_24h": None,
            "buy_volume_1h": None,
            "sell_volume_1h": None,
            "buy_pressure": None,
            "price": None,
            "sources": {"bitquery": None},
        }
        if not self.api_key:
            return result

        data = await self.fetch_token_stats(mint)
        result["sources"]["bitquery"] = data
        if not data or "data" not in data:
            return result

        trades = (
            data.get("data", {})
            .get("Solana", {})
            .get("DEXTradeByTokens", [])
        )
        if not trades:
            return result

        buy_vol = 0.0
        sell_vol = 0.0
        prices: list[float] = []
        for trade in trades:
            t = trade.get("Trade", {})
            buy_amt = float(t.get("Buy", {}).get("Amount", 0) or 0)
            sell_amt = float(t.get("Sell", {}).get("Amount", 0) or 0)
            buy_price = float(t.get("Buy", {}).get("Price", 0) or 0)

            if buy_price > 0:
                prices.append(buy_price)

            # Heuristic: if base-currency side is SOL/stable, the "Amount"
            # of the Buy side approximates volume in token terms.
            buy_vol += buy_amt
            sell_vol += sell_amt

        total_vol = buy_vol + sell_vol
        result["volume_24h"] = total_vol
        result["buy_volume_1h"] = buy_vol
        result["sell_volume_1h"] = sell_vol
        result["buy_pressure"] = (
            buy_vol / total_vol if total_vol > 0 else 0.5
        )
        if prices:
            result["price"] = prices[-1]  # latest trade price

        return result