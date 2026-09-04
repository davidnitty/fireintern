"""StockYard client — stock-paired memecoin discovery on Robinhood Chain.

Stockyard (https://stockyard.rhps.fun) maps Robinhood stock tokens (COST,
NVDA, TSLA...) to every memecoin launched against them ("stock pair" trading,
e.g. the Nvidia-office-dog paired with NVDA). Its public map.json exposes the
whole graph with live liquidity/volume per memecoin.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from memecoin_alert_bot.utils.helpers import fetch_json

logger = logging.getLogger(__name__)

MAP_URL = "https://stockyard.rhps.fun/api/map.json"


class StockyardClient:
    """Fetches the stock->memecoin pair map from StockYard."""

    def __init__(self, session: aiohttp.ClientSession | None = None):
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

    async def get_map(self) -> dict[str, Any] | None:
        """Fetch the full stock-pair map. Returns the parsed JSON or None."""
        return await fetch_json(self.session, MAP_URL, timeout=25)

    async def get_paired_memecoins(self) -> list[dict[str, Any]]:
        """Flatten the map into memecoin dicts, each carrying its stock context.

        Row shape (observed live): rows[] = stock tokens with
            t (stock ticker), a (stock address), p (price),
            sl/sv/ml/mv (stock liquidity/volume/mc), n (holders),
            m[] = paired MEMECOINS: s symbol, a address, l liquidity,
            v 24h volume, tx txns, cs/vs change+volume windows,
            mc market cap, age (hours), u dexscreener pool URL, lp launchpad
        """
        data = await self.get_map()
        if not isinstance(data, dict):
            return []

        memes: list[dict[str, Any]] = []
        by_mint: dict[str, dict[str, Any]] = {}
        for row in data.get("rows", []) or []:
            stock_ticker = row.get("t") or ""
            stock_addr = row.get("a") or ""
            for m in row.get("m", []) or []:
                address = m.get("a") or ""
                if not address:
                    continue
                pair_id = ""
                link = m.get("u") or ""
                if "/robinhood/" in link:
                    tail = link.rsplit("/", 1)[-1].split("?")[0]
                    if len(tail) >= 64:
                        pair_id = tail[-64:]
                entry = {
                    "mint": address,
                    "symbol": m.get("s") or "UNKNOWN",
                    "name": f"{m.get('s', '')} ({stock_ticker} pair)".strip(),
                    "market_cap": _f(m.get("mc")),
                    "liquidity": _f(m.get("l")),
                    "volume_24h": _f(m.get("v")),
                    "tx_count": _i(m.get("tx")),
                    "age_hours": _f(m.get("age")),
                    "change_series": m.get("cs") or [],
                    "launchpad": m.get("lp") or "",
                    "stock_ticker": stock_ticker,
                    "stock_address": stock_addr,
                    "pool_id": pair_id,
                    "raw": m,
                }
                # The same memecoin is often listed under several pools /
                # launchpads — keep only its most liquid entry.
                existing = by_mint.get(address)
                if existing is None or (entry["volume_24h"] or 0) > (existing["volume_24h"] or 0):
                    by_mint[address] = entry
        memes = list(by_mint.values())
        return memes


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None