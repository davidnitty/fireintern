"""Entry point for the Memecoin Alert Bot."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

import aiohttp

from memecoin_alert_bot.bot.telegram import TelegramBot
from memecoin_alert_bot.config import get_settings
from memecoin_alert_bot.data.dexscreener import DexScreenerClient
from memecoin_alert_bot.data.pumpfun import PumpFunClient
from memecoin_alert_bot.data.pumpportal import PumpPortalClient
from memecoin_alert_bot.data.rugcheck import RugcheckClient
from memecoin_alert_bot.data.solscan import SolscanClient
from memecoin_alert_bot.data.twitter import XApiClient
from memecoin_alert_bot.engine import detectors, normalizer, scorer
from memecoin_alert_bot.engine.models import Alert
from memecoin_alert_bot.storage.sqlite import Storage
from memecoin_alert_bot.utils.helpers import setup_logging

logger = logging.getLogger("memecoin_alert_bot")


class BotApp:
    """Orchestrates data ingestion, signal detection, and Telegram alerts."""

    def __init__(self) -> None:
        self.settings = get_settings()
        setup_logging(self.settings.log_level)
        self.storage = Storage()
        self.telegram = TelegramBot(self.settings, self.storage)
        self.session: aiohttp.ClientSession | None = None
        self._clients: list[Any] = []
        self._shutdown_event = asyncio.Event()
        self._semaphore = asyncio.Semaphore(5)  # limit concurrent enrichment

    async def _init_clients(self) -> None:
        self.session = aiohttp.ClientSession()
        self.pumpfun = PumpFunClient(self.session)
        self.dexscreener = DexScreenerClient(self.session)
        self.rugcheck = RugcheckClient(self.settings.rugcheck_api_key, self.session)
        self.solscan = SolscanClient(self.settings.solscan_api_key, self.session)
        self.x_api = XApiClient(self.settings.x_bearer_token, self.session)
        self._clients = [
            self.pumpfun,
            self.dexscreener,
            self.rugcheck,
            self.solscan,
            self.x_api,
        ]

    async def _close_clients(self) -> None:
        for client in self._clients:
            try:
                await client.close()
            except Exception:
                logger.exception("Error closing data client")
        self._clients.clear()
        if self.session and not self.session.closed:
            await self.session.close()

    async def _enrich_coin(self, coin) -> None:
        """Fetch and merge metadata from all configured sources."""
        async with self._semaphore:
            mint = coin.mint

            tasks = []
            if self.settings.enable_pumpfun_rest:
                tasks.append(self.pumpfun.enrich_coin(mint, {}))
            if self.settings.enable_dexscreener:
                tasks.append(self.dexscreener.enrich_coin(mint, {}))
            if self.settings.enable_rugcheck:
                tasks.append(self.rugcheck.enrich_coin(mint, {}))
            if self.settings.enable_solscan:
                tasks.append(self.solscan.enrich_coin(mint, {}))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.debug("Enrichment error for %s: %s", mint, result)
                    continue
                if isinstance(result, dict):
                    coin = normalizer.merge_enrichment(coin, result)

            # Narrative enrichment from X (with fallback when no key)
            try:
                x_result = await self.x_api.narrative_mentions(coin.symbol, coin.name)
                coin.sources["x_api"] = x_result.get("sources", {}).get("x_api")
                # If API returned engagement, bump narrative strength slightly.
                if x_result.get("count", 0) > 0:
                    coin.narrative_strength = min(1.0, coin.narrative_strength + 0.2)
            except Exception:
                logger.debug("X API enrichment skipped for %s", mint)

            return coin

    async def _handle_new_token(self, event: dict[str, Any]) -> None:
        """Process a new token event end-to-end."""
        mint = event.get("mint") or event.get("token")
        if not mint:
            return

        try:
            coin = normalizer.create_from_pumpportal(event)
            coin = await self._enrich_coin(coin)

            # Persist for history/backtesting
            await self.storage.upsert_coin(coin)

            signals = detectors.run_all(coin)
            score = scorer.score_coin(coin, signals)

            alert = Alert(coin=coin, signals=signals, score=score)

            if await self.storage.is_on_cooldown(mint, self.settings.alert_cooldown_seconds):
                logger.debug("Alert for %s is on cooldown", mint)
                return

            # Record snapshot regardless for backtesting.
            await self.storage.record_backtest_snapshot(coin, score)
            await self.storage.save_alert(alert)

            sent = await self.telegram.send_alert(alert)
            if sent:
                await self.storage.set_cooldown(mint)
                logger.info(
                    "Alert sent: %s (%s) | %s | score=%.2f",
                    coin.symbol,
                    mint,
                    score.verdict.value,
                    score.composite_score,
                )
        except Exception:
            logger.exception("Failed to process token %s", mint)

    async def run(self) -> None:
        """Start the bot and run until shutdown."""
        await self.storage.connect()
        await self._init_clients()

        # Telegram setup
        await self.telegram.setup()
        telegram_task = asyncio.create_task(self.telegram.start())

        # PumpPortal listener setup
        pumpportal = PumpPortalClient(token_handler=self._handle_new_token)

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
            except NotImplementedError:
                pass  # Windows does not support add_signal_handler for SIGTERM

        logger.info("Starting Memecoin Alert Bot")
        pumpportal_task = pumpportal.start()

        try:
            await self._shutdown_event.wait()
        finally:
            logger.info("Shutting down...")
            await pumpportal.stop()
            telegram_task.cancel()
            try:
                await telegram_task
            except asyncio.CancelledError:
                pass
            await self.telegram.stop()
            await self._close_clients()
            await self.storage.close()

    def _request_shutdown(self) -> None:
        logger.info("Shutdown signal received")
        self._shutdown_event.set()


def main() -> None:
    try:
        asyncio.run(BotApp().run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
