"""Pons launchpad indexer on Robinhood Chain."""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from eth_abi import decode as eth_abi_decode
from eth_utils import to_checksum_address

from memecoin_alert_bot.data.robinhood import (
    BLOCK_TIME_SECONDS,
    CHAIN_ID as ROBINHOOD_CHAIN_ID,
    WETH,
    RobinhoodChainClient,
    estimate_market_cap,
)
from memecoin_alert_bot.engine.models import CoinData, SafetyInfo

logger = logging.getLogger(__name__)

ACTIVE_FACTORY = "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB"
LEGACY_FACTORY = "0x0c37a24F5D23A486FA692d1500881d698B1F77a4"
FACTORY_START_BLOCKS = {
    ACTIVE_FACTORY.lower(): 8_991_118,
    LEGACY_FACTORY.lower(): 8_600_612,
}
TOKEN_LAUNCHED_TOPIC0 = (
    "0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a"
)



class PonsIndexer:
    """Poll Pons factory contracts for TokenLaunched events."""

    def __init__(
        self,
        client: RobinhoodChainClient,
        token_handler: Callable[[CoinData], Any] | None = None,
    ):
        self.client = client
        self.token_handler = token_handler
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._backfill_chunk = 2_000  # blocks per eth_getLogs query

    async def _handle(self, coin: CoinData) -> None:
        if self.token_handler is None:
            return
        try:
            if inspect.iscoroutinefunction(self.token_handler):
                await self.token_handler(coin)
            else:
                self.token_handler(coin)
        except Exception:
            logger.exception("Error in Pons token handler")

    async def _process_log(self, log: dict[str, Any]) -> CoinData | None:
        """Decode a TokenLaunched event and build a CoinData object."""
        topics = log.get("topics", [])
        data = log.get("data", "0x")
        if len(topics) < 3:
            return None

        token = "0x" + topics[1][-40:]
        deployer = "0x" + topics[2][-40:]
        factory = log.get("address", "").lower()

        try:
            raw = bytes.fromhex(data.replace("0x", ""))
            decoded = eth_abi_decode(
                [
                    "address",
                    "address",
                    "address",
                    "uint256",
                    "uint256",
                    "uint256",
                    "uint256",
                ],
                raw,
            )
            pair_token, pool, *_ = decoded
        except Exception as exc:
            logger.debug("Failed to decode TokenLaunched data: %s", exc)
            pair_token = ""
            pool = ""

        token_meta = await self.client.fetch_token_metadata(token)
        if not token_meta.get("name") and not token_meta.get("symbol"):
            logger.debug("Skipping token %s with unreadable metadata", token)
            return None

        socials = token_meta.get("socials", {})
        pool_addr = token_meta.get("pool_address") or (pool.hex() if isinstance(pool, bytes) else (str(pool) if pool else ""))
        pair_addr = pair_token.hex() if isinstance(pair_token, bytes) else (str(pair_token) if pair_token else WETH)

        price_info = await self.client.fetch_pool_price(pool_addr, token, pair_addr)

        swap_info = await self.client.fetch_recent_swaps(pool_addr, token, pair_addr)

        # Real coin age from launch block (0.25s block time on Arbitrum Orbit).
        launch_block = int(log.get("blockNumber", "0x0") or "0x0", 16)
        price_in_pair = price_info.get("price")
        market_cap = estimate_market_cap(price_in_pair, token_meta.get("total_supply"))

        age_seconds = None
        if launch_block > 0:
            try:
                latest = await self.client.get_block_number()
                age_seconds = max(0, (latest - launch_block)) * BLOCK_TIME_SECONDS
                age_seconds = int(age_seconds)
            except Exception:
                pass

        coin = CoinData(
            mint=token,
            chain="robinhood",
            chain_id=ROBINHOOD_CHAIN_ID,
            symbol=token_meta.get("symbol", "UNKNOWN"),
            name=token_meta.get("name", ""),
            description=token_meta.get("description", ""),
            deployer=deployer,
            dev_wallet=deployer,
            price=price_in_pair,
            market_cap=market_cap,
            # Chain-native pair amounts are directional-only; do not mislabel
            # a ~42 minute observation as USD 1h/24h volume.
            buy_pressure=swap_info.get("buy_pressure", 0.5),
            buys_5m=swap_info.get("buys", 0),
            sells_5m=swap_info.get("sells", 0),
            flow_data_quality="directional_only",
            age_seconds=age_seconds,
            pool_address=pool_addr,
            pair_token=pair_addr,
            social_links=socials,
            created_at=datetime.now(timezone.utc),
            sources={
                "pons": {
                    "factory": factory,
                    "tx_hash": log.get("transactionHash"),
                    "block": log.get("blockNumber"),
                }
            },
        )

        # Copy social links to top-level fields for formatter reuse.
        # Map supported social fields; keep the rest in social_links.
        for key in ["twitter", "telegram", "website"]:
            if socials.get(key):
                setattr(coin, key, socials[key])

        coin.safety = SafetyInfo(lp_locked=True)  # Pons locks LP by design.
        return coin

    async def poll_once(
        self,
        from_block: int,
        to_block: int | None = None,
        factories: list[str] | None = None,
    ) -> int:
        """Poll for TokenLaunched events in a block range. Returns the last processed block."""
        factories = factories or [ACTIVE_FACTORY, LEGACY_FACTORY]
        if to_block is None:
            to_block = await self.client.get_block_number()
        if to_block <= from_block:
            return from_block

        # Pons event has 3 indexed params → topics length = 4
        topics: list[Any] = [TOKEN_LAUNCHED_TOPIC0]

        logs = await self.client.get_logs(
            from_block, to_block, factories, topics
        )
        logger.info("Pons poll blocks %d-%d: %d new tokens", from_block, to_block, len(logs))
        for log in logs:
            try:
                coin = await self._process_log(log)
                if coin:
                    await self._handle(coin)
            except (IndexError, KeyError, ValueError, TypeError) as exc:
                # A malformed or newer event must not abort the whole block batch.
                logger.debug("Skipping malformed Pons log: %s", exc)
            except Exception:
                logger.exception("Skipping unexpected Pons log error")
        return to_block

    async def run(
        self,
        start_block: int | None = None,
        interval_seconds: float = 10.0,
        get_last_block: Callable[[], Any] | None = None,
        save_last_block: Callable[[int], Any] | None = None,
    ) -> None:
        """Continuously poll for new Pons tokens."""
        if start_block is None:
            start_block = await self.client.get_block_number() - 100

        last_block = start_block
        while not self._stop_event.is_set():
            try:
                latest = await self.client.get_block_number()
                upper = min(latest, last_block + self._backfill_chunk)
                last_block = await self.poll_once(last_block + 1, upper)
                if save_last_block:
                    await save_last_block(last_block)
                if upper >= latest:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), interval_seconds)
                    except asyncio.TimeoutError:
                        pass
            except Exception as exc:
                logger.warning("Pons indexer error: %s", exc)
                await asyncio.sleep(interval_seconds)

    def start(
        self,
        start_block: int | None = None,
        interval_seconds: float = 10.0,
        get_last_block: Callable[[], Any] | None = None,
        save_last_block: Callable[[int], Any] | None = None,
    ) -> asyncio.Task:
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self.run(start_block, interval_seconds, get_last_block, save_last_block)
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

