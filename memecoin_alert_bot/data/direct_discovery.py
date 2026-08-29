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

from memecoin_alert_bot.data.dexscreener import import_time
from memecoin_alert_bot.data.robinhood import (
    CHAIN_ID as ROBINHOOD_CHAIN_ID,
    RobinhoodChainClient,
)
from memecoin_alert_bot.engine.models import CoinData, SafetyInfo

logger = logging.getLogger(__name__)

# DexScreener chainId for Robinhood Chain (verified from the live API —
# it is literally "robinhood", NOT "robinhoodchain").
DEXSCREENER_SLUG = "robinhood"
MIN_LIQUIDITY_USD = 5_000.0


class DirectDiscoveryIndexer:
    """Discover fresh Robinhood Chain tokens via DexScreener's profile feed.

    Catches tokens deployed directly by wallets (not via Pons/Noxa factories),
    including Uniswap v4 launches that factory-only indexers never see.
    """

    def __init__(
        self,
        dexscreener,  # DexScreenerClient
        robinhood: RobinhoodChainClient,
        token_handler: Callable[[CoinData], Any] | None = None,
        max_age_minutes: int = 180,
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
            profiles = await self.dexscreener.get_latest_profiles()
        except Exception as exc:
            logger.warning("Direct discovery poll failed: %s", exc)
            return 0

        mints = [
            p.get("tokenAddress")
            for p in profiles
            if p.get("chainId") == DEXSCREENER_SLUG and p.get("tokenAddress")
        ]
        if not mints:
            return 0

        emitted = 0
        for mint in mints:
            if mint in self._seen_pairs:
                continue
            self._seen_pairs.add(mint)
            if len(self._seen_pairs) > 5_000:
                # Bound memory; old addresses age out naturally by set size.
                self._seen_pairs = set(list(self._seen_pairs)[-2_000:])

            try:
                pair = await self._best_pair(mint)
                if pair is None:
                    continue
                coin = await self._build_coin(mint, pair)
                if coin is not None:
                    emitted += 1
                    await self._handle(coin)
            except Exception as exc:
                logger.warning("Direct discovery token %s failed: %s", mint, exc)
        return emitted

    async def _best_pair(self, mint: str) -> dict[str, Any] | None:
        """Highest-liquidity robinhood pair for the mint, with filters applied."""
        data = await self.dexscreener.fetch_token_pairs(mint)
        pairs = data.get("pairs", []) if data else []
        chain_pairs = [p for p in pairs if p.get("chainId") == DEXSCREENER_SLUG]
        if not chain_pairs:
            return None
        best = max(
            chain_pairs,
            key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0),
            default=None,
        )
        if best is None:
            return None
        # Liquidity floor skips dust pools and spam launches.
        liquidity = float(best.get("liquidity", {}).get("usd", 0) or 0)
        if liquidity < MIN_LIQUIDITY_USD:
            return None
        created = best.get("pairCreatedAt")
        if created:
            age_min = (import_time() * 1000 - int(created)) / 60_000
            if age_min > self.max_age_minutes:
                return None
        return best

    async def _build_coin(self, mint: str, pair: dict[str, Any]) -> CoinData | None:
        pair_id = pair.get("pairAddress") or pair.get("pairId") or ""
        base = pair.get("baseToken") or {}
        volume = pair.get("volume", {})
        txns = pair.get("txns", {})
        price_change = pair.get("priceChange", {})
        quote = pair.get("quoteToken") or {}
        info = pair.get("info") or {}
        liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)

        created_ms = pair.get("pairCreatedAt")
        age_seconds = None
        if created_ms:
            age_seconds = max(0, int(import_time() - int(created_ms) / 1000))

        # On-chain verification: ownership via generic ERC-20 owner().
        meta = await self.robinhood.fetch_token_metadata(mint) or {}
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
        return coin

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
