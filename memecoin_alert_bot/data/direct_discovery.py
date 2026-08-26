"""Direct-token discovery on Robinhood Chain via DexScreener new pairs.

Catches tokens deployed directly by wallets (not via Pons/Noxa factories),
such as Uniswap v4 launches that factory-only indexers never see.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from memecoin_alert_bot.data.robinhood import (
    CHAIN_ID as ROBINHOOD_CHAIN_ID,
    RobinhoodChainClient,
)
from memecoin_alert_bot.engine.models import CoinData, SafetyInfo

logger = logging.getLogger(__name__)

# DexScreener chain slug for Robinhood Chain.
DEXSCREENER_SLUG = "robinhoodchain"
MIN_LIQUIDITY_USD = 5_000.0


class DirectDiscoveryIndexer:
    """Poll DexScreener for fresh Robinhood Chain pairs and emit CoinData."""

    def __init__(
        self,
        dexscreener,  # DexScreenerClient
        robinhood: RobinhoodChainClient,
        token_handler: Callable[[CoinData], Any] | None = None,
        max_age_minutes: int = 20,
    ):
        self.dexscreener = dexscreener
        self.robinhood = robinhood
        self.token_handler = token_handler
        self.max_age_minutes = max_age_minutes
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._seen_pairs: set[str] = set()

    async def _handle(self, coin: CoinData) -> None:
        if self.token_handler is None:
            return
        try:
            if inspect.iscoroutinefunction(self.token_handler):
                await self.token_handler(coin)
            else:
                self.token_handler(coin)
        except Exception:
            logger.exception("Error in direct-discovery token handler")

    async def poll_once(self) -> int:
        """One discovery pass; returns number of new tokens emitted."""
        try:
            pairs = await self.dexscreener.get_new_pairs(
                DEXSCREENER_SLUG, max_age_minutes=self.max_age_minutes
            )
        except Exception as exc:
            logger.warning("Direct discovery poll failed: %s", exc)
            return 0

        emitted = 0
        for pair in pairs:
            pair_id = pair.get("pairAddress") or pair.get("pairId") or ""
            base = pair.get("baseToken") or {}
            mint = base.get("address") or ""
            if not mint or mint in self._seen_pairs:
                continue

            # Liquidity floor skips dust pools and spam launches.
            liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
            if liquidity < MIN_LIQUIDITY_USD:
                continue

            self._seen_pairs.add(mint)
            if len(self._seen_pairs) > 5_000:
                # Bound memory; old addresses age out naturally by set size.
                self._seen_pairs = set(list(self._seen_pairs)[-2_000:])

            volume = pair.get("volume", {})
            txns = pair.get("txns", {})
            price_change = pair.get("priceChange", {})
            quote = pair.get("quoteToken") or {}
            info = pair.get("info") or {}

            created_ms = pair.get("pairCreatedAt")
            age_seconds = None
            if created_ms:
                import time

                age_seconds = max(0, int(time.time() - int(created_ms) / 1000))

            # On-chain verification: deployer/ownership via generic ERC-20.
            meta = await self.robinhood.fetch_token_metadata(mint)
            socials = meta.get("socials", {}) or {}
            if info.get("socials"):
                for s in info["socials"]:
                    stype = s.get("type")
                    if stype in ("twitter", "telegram", "website"):
                        socials.setdefault(stype, s.get("url", ""))

            coin = CoinData(
                mint=mint,
                chain="robinhood",
                chain_id=ROBINHOOD_CHAIN_ID,
                symbol=base.get("symbol") or meta.get("symbol") or "UNKNOWN",
                name=base.get("name") or meta.get("name") or "",
                description=meta.get("description", ""),
                dev_wallet=meta.get("deployer", "") or pair.get("creator", ""),
                deployer=meta.get("deployer", ""),
                price=float(pair.get("priceUsd", 0) or 0) or None,
                market_cap=float(pair.get("marketCap", 0) or 0) or None,
                liquidity=liquidity,
                volume_24h=float(volume.get("h24", 0) or 0),
                volume_5m=float(volume.get("m5", 0) or 0),
                volume_1h=float(volume.get("h1", 0) or 0),
                buys_5m=int(txns.get("m5", {}).get("buys", 0) or 0),
                sells_5m=int(txns.get("m5", {}).get("sells", 0) or 0),
                buys_1h=int(txns.get("h1", {}).get("buys", 0) or 0),
                sells_1h=int(txns.get("h1", {}).get("sells", 0) or 0),
                price_change_5m=float(price_change.get("m5", 0) or 0),
                price_change_1h=float(price_change.get("h1", 0) or 0),
                age_seconds=age_seconds,
                pool_address=pair_id if len(str(pair_id)) == 42 else None,
                pair_token=quote.get("address"),
                social_links=socials,
                created_at=datetime.now(timezone.utc),
                sources={"direct_discovery": pair},
            )
            for key in ["twitter", "telegram", "website"]:
                if socials.get(key):
                    setattr(coin, key, socials[key])

            coin.safety = SafetyInfo(
                lp_locked=None,
                mint_authority_enabled=None if meta.get("ownership_renounced") is None else not meta.get("ownership_renounced"),
                freeze_authority_enabled=False,
            )
            coin.flow_data_quality = "verified_usd"
            emitted += 1
            await self._handle(coin)
        return emitted

    async def run(self, interval_seconds: float = 30.0) -> None:
        logger.info("Direct discovery started (max pair age %dm)", self.max_age_minutes)
        while not self._stop_event.is_set():
            try:
                count = await self.poll_once()
                if count:
                    logger.info("Direct discovery: %d new tokens", count)
            except Exception as exc:
                logger.warning("Direct discovery error: %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), interval_seconds)
            except asyncio.TimeoutError:
                pass

    def start(self, interval_seconds: float = 30.0) -> asyncio.Task:
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run(interval_seconds))
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
