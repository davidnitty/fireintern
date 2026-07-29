"""Noxa launchpad indexer on Robinhood Chain.

Noxa does not expose a token-creation event we rely on.  Instead we poll the
on-chain token registry directly:
  - allTokensLength() -> uint256
  - allTokens(uint256 index) -> address
Because the factory keeps an append-only list, new tokens appear as new indices.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from eth_abi import encode as eth_abi_encode
from web3 import Web3

from memecoin_alert_bot.data.robinhood import (
    CHAIN_ID as ROBINHOOD_CHAIN_ID,
    WETH,
    RobinhoodChainClient,
)
from memecoin_alert_bot.engine.models import CoinData, SafetyInfo

logger = logging.getLogger(__name__)

LAUNCH_FACTORY = "0xDd84fDdEA1206115B37dbBC0ba5721530E1bA9C5"
LAUNCH_LOCKER = "0x9A6931E371b62048C7543C7002C99D83685BD44d"
TREASURY = "0x4977307cF8fa1fb5Ce45873717164c872BAD6f23"

ALL_TOKENS_LENGTH_SELECTOR = "0x" + Web3.keccak(text="allTokensLength()")[:4].hex()
ALL_TOKENS_SELECTOR = "0x" + Web3.keccak(text="allTokens(uint256)")[:4].hex()


class NoxaIndexer:
    """Poll Noxa factory token registry for new tokens."""

    def __init__(
        self,
        client: RobinhoodChainClient,
        factory: str | None = None,
        token_handler: Callable[[CoinData], Any] | None = None,
    ):
        self.client = client
        self.factory = (factory or LAUNCH_FACTORY).lower()
        self.token_handler = token_handler
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_count = 0

    async def _handle(self, coin: CoinData) -> None:
        if self.token_handler is None:
            return
        try:
            if inspect.iscoroutinefunction(self.token_handler):
                await self.token_handler(coin)
            else:
                self.token_handler(coin)
        except Exception:
            logger.exception("Error in Noxa token handler")

    async def _call_factory(self, data: str) -> str | None:
        result = await self.client._rpc(
            "eth_call",
            [{"to": self.factory, "data": data}, "latest"],
        )
        return result if isinstance(result, str) else None

    async def _get_token_count(self) -> int:
        result = await self._call_factory(ALL_TOKENS_LENGTH_SELECTOR)
        return int(result, 16) if result else 0

    async def _get_token_address(self, index: int) -> str | None:
        data = ALL_TOKENS_SELECTOR + eth_abi_encode(["uint256"], [index]).hex()
        result = await self._call_factory(data)
        if not result:
            return None
        return "0x" + result[-40:]

    async def _process_token(self, token: str) -> CoinData | None:
        token = token.lower()
        token_meta = await self.client.fetch_token_metadata(token)
        if not token_meta.get("name") and not token_meta.get("symbol"):
            logger.debug("Skipping Noxa token %s with unreadable metadata", token)
            return None

        socials = token_meta.get("socials", {})
        pool_addr = token_meta.get("pool_address", "")

        price_info = await self.client.fetch_pool_price(pool_addr, token, WETH)

        coin = CoinData(
            mint=token,
            chain="robinhood",
            chain_id=ROBINHOOD_CHAIN_ID,
            symbol=token_meta.get("symbol", "UNKNOWN"),
            name=token_meta.get("name", ""),
            description=token_meta.get("description", ""),
            dev_wallet="",
            price=price_info.get("price"),
            pool_address=pool_addr,
            pair_token=WETH,
            social_links=socials,
            created_at=datetime.now(timezone.utc),
            sources={
                "noxa": {
                    "factory": self.factory,
                }
            },
        )

        for key in ["twitter", "telegram", "website"]:
            if socials.get(key):
                setattr(coin, key, socials[key])

        coin.safety = SafetyInfo(lp_locked=True)  # Noxa locks LP by design.
        return coin

    async def poll_once(self) -> int:
        """Check registry length and process any new tokens."""
        count = await self._get_token_count()
        if count <= self._last_count:
            return count

        new_count = count - self._last_count
        logger.info(
            "Noxa registry: %d total, %d new since last poll",
            count,
            new_count,
        )
        for index in range(self._last_count, count):
            token = await self._get_token_address(index)
            if not token:
                continue
            coin = await self._process_token(token)
            if coin:
                await self._handle(coin)
        return count

    async def run(
        self,
        interval_seconds: float = 15.0,
        get_last_count: Callable[[], Any] | None = None,
        save_last_count: Callable[[int], Any] | None = None,
    ) -> None:
        """Continuously poll Noxa registry for newly launched tokens."""
        if self._last_count == 0:
            # Initialize to current count so we don't backfill the whole history on first run.
            self._last_count = await self._get_token_count()
            logger.info("Noxa starting at registry index %d", self._last_count)
            if save_last_count:
                await save_last_count(self._last_count)

        while not self._stop_event.is_set():
            try:
                self._last_count = await self.poll_once()
                if save_last_count:
                    await save_last_count(self._last_count)

                try:
                    await asyncio.wait_for(self._stop_event.wait(), interval_seconds)
                except asyncio.TimeoutError:
                    pass
            except Exception as exc:
                logger.warning("Noxa indexer error: %s", exc)
                await asyncio.sleep(interval_seconds)

    def start(
        self,
        interval_seconds: float = 15.0,
        get_last_count: Callable[[], Any] | None = None,
        save_last_count: Callable[[int], Any] | None = None,
    ) -> asyncio.Task:
        self._stop_event.clear()
        if get_last_count:
            try:
                self._last_count = get_last_count()
            except Exception:
                pass
        self._task = asyncio.create_task(
            self.run(interval_seconds, get_last_count, save_last_count)
        )
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
