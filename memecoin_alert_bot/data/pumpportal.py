"""PumpPortal WebSocket client for real-time pump.fun token events."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import websockets

logger = logging.getLogger(__name__)

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"


class PumpPortalClient:
    """Listen to pump.fun token creation and trade events via PumpPortal."""

    def __init__(
        self,
        token_handler: Callable[[dict[str, Any]], Any] | None = None,
        trade_handler: Callable[[dict[str, Any]], Any] | None = None,
    ):
        self.token_handler = token_handler or self._default_token_handler
        self.trade_handler = trade_handler or self._default_trade_handler
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _default_token_handler(self, data: dict[str, Any]) -> None:
        logger.debug("PumpPortal token: %s", data.get("mint"))

    async def _default_trade_handler(self, data: dict[str, Any]) -> None:
        logger.debug("PumpPortal trade: %s", data.get("mint"))

    async def _handle_message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return

        event_type = message.get("txType")
        if event_type == "create":
            await self._maybe_run(self.token_handler, message)
        elif event_type in ("buy", "sell"):
            await self._maybe_run(self.trade_handler, message)

    async def _maybe_run(self, handler: Callable, data: dict[str, Any]) -> None:
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler(data)
            else:
                handler(data)
        except Exception:
            logger.exception("Error in PumpPortal handler")

    @staticmethod
    def _normalize_token_event(data: dict[str, Any]) -> dict[str, Any]:
        """Convert a PumpPortal create event into a CoinData-like dict."""
        return {
            "mint": data.get("mint") or data.get("token") or "",
            "symbol": data.get("symbol", "UNKNOWN"),
            "name": data.get("name", ""),
            "description": "",
            "dev_wallet": data.get("traderPublicKey", ""),
            "price_sol": float(data.get("initialBuy", 0) or 0),
            "market_cap": float(data.get("marketCapSol", 0) or 0),
            "volume_24h": None,
            "liquidity": None,
            "holders": None,
            "age_seconds": 0,
            "tokenized_agent": False,
            "social_links": {},
            "sources": {"pumpportal": data},
        }

    async def connect(self, subscribe_trades: bool = False) -> None:
        """Connect and subscribe to token events. Reconnect on failure."""
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(PUMPPORTAL_WS) as ws:
                    logger.info("Connected to PumpPortal WebSocket")
                    # Subscribe to new token events
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    if subscribe_trades:
                        await ws.send(
                            json.dumps({"method": "subscribeTokenTrade", "keys": []})
                        )
                    while not self._stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                            if isinstance(raw, bytes):
                                raw = raw.decode("utf-8")
                            await self._handle_message(raw)
                        except asyncio.TimeoutError:
                            try:
                                pong = await ws.ping()
                                await asyncio.wait_for(pong, timeout=10)
                            except Exception:
                                break
            except websockets.ConnectionClosed:
                logger.warning("PumpPortal connection closed, reconnecting...")
            except Exception as exc:
                logger.warning("PumpPortal error: %s", exc)
            await asyncio.sleep(5)

    def start(self) -> asyncio.Task:
        """Start the WebSocket listener as a background task."""
        self._stop_event.clear()
        self._task = asyncio.create_task(self.connect())
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
