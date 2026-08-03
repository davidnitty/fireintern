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
from memecoin_alert_bot.data.bitquery import BitqueryClient
from memecoin_alert_bot.data.bubblemaps import BubblemapsClient
from memecoin_alert_bot.data.dexscreener import DexScreenerClient
from memecoin_alert_bot.data.noxa import NoxaIndexer
from memecoin_alert_bot.data.pons import PonsIndexer
from memecoin_alert_bot.data.pumpfun import PumpFunClient
from memecoin_alert_bot.data.pumpportal import PumpPortalClient
from memecoin_alert_bot.data.robinhood import RobinhoodChainClient
from memecoin_alert_bot.data.rugcheck import RugcheckClient
from memecoin_alert_bot.data.solscan import SolscanClient
from memecoin_alert_bot.data.twitter import XApiClient
from memecoin_alert_bot.engine import detectors, normalizer, scorer
from memecoin_alert_bot.engine.models import Alert, CoinData
from memecoin_alert_bot.engine.nlp import get_narrative_analyzer
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
        self._solana_semaphore = asyncio.Semaphore(5)  # limit concurrent enrichment
        self._pons_indexer: PonsIndexer | None = None
        self._noxa_indexer: NoxaIndexer | None = None
        self._symbol_cooldowns: dict[str, float] = {}  # symbol -> last alert timestamp

    async def _init_clients(self) -> None:
        self.session = aiohttp.ClientSession()
        self.pumpfun = PumpFunClient(self.session)
        self.dexscreener = DexScreenerClient(self.session)
        self.rugcheck = RugcheckClient(self.settings.rugcheck_api_key, self.session)
        self.solscan = SolscanClient(self.settings.solscan_api_key, self.session)
        self.x_api = XApiClient(self.settings.x_bearer_token, self.session)
        self.robinhood = RobinhoodChainClient(self.settings.robinhood_rpc_url, self.session)
        self.bubblemaps = BubblemapsClient(self.settings.bubblemaps_api_key, self.session)
        self.bitquery = BitqueryClient(self.settings.bitquery_api_key, self.session)
        self._clients = [
            self.pumpfun,
            self.dexscreener,
            self.rugcheck,
            self.solscan,
            self.x_api,
            self.robinhood,
            self.bubblemaps,
            self.bitquery,
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

    async def _enrich_solana_coin(self, coin) -> None:
        """Fetch and merge metadata from all configured Solana sources."""
        async with self._solana_semaphore:
            mint = coin.mint

            # Bitquery: primary data source for volume, price, buy pressure
            tasks = []
            bitquery_task = None
            if self.settings.bitquery_api_key:
                bitquery_task = asyncio.create_task(self.bitquery.enrich_coin(mint))
                tasks.append(bitquery_task)

            # DexScreener and pump.fun as fallbacks (always run alongside)
            if self.settings.enable_pumpfun_rest:
                tasks.append(self.pumpfun.enrich_coin(mint, {}))
            if self.settings.enable_dexscreener:
                tasks.append(self.dexscreener.enrich_coin(mint, {}))

            # Safety / cluster
            if self.settings.rugcheck_api_key:
                tasks.append(self.rugcheck.enrich_coin(mint, {}))
            if self.settings.solscan_api_key:
                tasks.append(self.solscan.enrich_coin(mint, {}))
            if self.settings.bubblemaps_api_key:
                tasks.append(self.bubblemaps.enrich_coin(mint))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Apply Bitquery result first (it's the most reliable)
            if bitquery_task:
                bq_result = results[0]
                if not isinstance(bq_result, Exception) and isinstance(bq_result, dict):
                    coin = normalizer.merge_enrichment(coin, bq_result)
                off = 1
            else:
                off = 0

            for result in results[off:]:
                if isinstance(result, Exception):
                    logger.debug("Enrichment error for %s: %s", mint, result)
                    continue
                if isinstance(result, dict):
                    coin = normalizer.merge_enrichment(coin, result)

            # Narrative enrichment from X (with fallback when no key)
            try:
                x_result = await self.x_api.narrative_mentions(coin.symbol, coin.name)
                coin.sources["x_api"] = x_result.get("sources", {}).get("x_api")
                if x_result.get("count", 0) > 0:
                    coin.narrative_strength = min(1.0, coin.narrative_strength + 0.2)
            except Exception:
                logger.debug("X API enrichment skipped for %s", mint)

            return coin

    async def _handle_new_token(self, event: dict[str, Any]) -> None:
        """Process a new Solana token event end-to-end."""
        mint = event.get("mint") or event.get("token")
        if not mint:
            return

        try:
            coin = normalizer.create_from_pumpportal(event)
            coin = await self._enrich_solana_coin(coin)

            # Apply trade-tracker buy-pressure (Solana)
            trades = self._solana_trades.get(mint)
            if trades:
                total = trades.get("buy", 0) + trades.get("sell", 0)
                if total > 0:
                    coin.buy_pressure = trades.get("buy", 0) / total
                    # Estimate volume from trade count (rough)
                    if coin.volume_24h is None:
                        coin.volume_24h = total * 50  # ~$50 avg trade
            await self._evaluate_and_alert(coin)
        except Exception:
            logger.exception("Failed to process Solana token %s", mint)

    async def _handle_robinhood_token(self, coin: CoinData) -> None:
        """Process a Robinhood Chain token discovered by Pons or Noxa indexers."""
        try:
            await self._evaluate_and_alert(coin)
        except Exception:
            logger.exception("Failed to process Robinhood token %s", coin.mint)

    async def _evaluate_and_alert(self, coin: CoinData) -> None:
        """Run detectors, score, and optionally send a Telegram alert."""
        mint = coin.mint

        # ── Market-cap filter (strict) ──
        if coin.market_cap is None:
            logger.debug("Skipping %s — no market cap data", mint)
            return
        if coin.market_cap < self.settings.min_market_cap:
            logger.debug(
                "Skipping %s — MC $%.0f < min $%.0f",
                mint, coin.market_cap, self.settings.min_market_cap,
            )
            return

        # ── NLP narrative analysis (must run before buying-activity check) ──
        narrative_text = f"{coin.name} {coin.description}"
        na = get_narrative_analyzer()
        coin.narrative_keywords = na.extract_keywords(narrative_text)[:10]
        coin.narrative_strength = na.narrative_strength(narrative_text)
        coin.vamp_similarity = na.check_vamp_risk(narrative_text)
        na.add_token(coin.mint, narrative_text)

        # ── Buying-activity filter (strict — ONLY coins people are buying) ──
        has_volume = (
            (coin.volume_24h is not None and coin.volume_24h > 0)
            or (coin.buy_volume_1h is not None and coin.buy_volume_1h > 0)
        )
        if not has_volume:
            logger.debug("Skipping %s — no buying activity detected", mint)
            return

        # ── Per-symbol cooldown (catch name copycats) ──
        import time
        now = time.time()
        last = self._symbol_cooldowns.get(coin.symbol)
        if last and (now - last) < 60:
            logger.debug("Skipping %s — symbol %s on cooldown", mint, coin.symbol)
            return
        self._symbol_cooldowns[coin.symbol] = now

        await self.storage.upsert_coin(coin)

        signals = detectors.run_all(coin)
        score = scorer.score_coin(coin, signals)

        alert = Alert(coin=coin, signals=signals, score=score)

        if await self.storage.is_on_cooldown(mint, self.settings.alert_cooldown_seconds):
            logger.debug("Alert for %s is on cooldown", mint)
            return

        await self.storage.record_backtest_snapshot(coin, score)
        await self.storage.save_alert(alert)

        sent = await self.telegram.send_alert(alert)
        if sent:
            await self.storage.set_cooldown(mint)
            logger.info(
                "Alert sent: %s (%s on %s) | %s | score=%.2f",
                coin.symbol,
                mint,
                coin.chain,
                score.verdict.value,
                score.composite_score,
            )

    async def run(self) -> None:
        """Start the bot and run until shutdown."""
        await self.storage.connect()
        await self._init_clients()

        # Telegram setup
        await self.telegram.setup()
        telegram_task = asyncio.create_task(self.telegram.start())

        # PumpPortal listener setup (tokens + trades for Solana buying pressure)
        self._solana_trades: dict[str, dict[str, int]] = {}

        async def _handle_solana_trade(data: dict[str, Any]) -> None:
            mint = data.get("mint") or data.get("token")
            tx_type = data.get("txType", "")
            if mint and tx_type in ("buy", "sell"):
                tracker = self._solana_trades.setdefault(mint, {"buy": 0, "sell": 0})
                tracker[tx_type] = tracker.get(tx_type, 0) + 1

        pumpportal = PumpPortalClient(
            token_handler=self._handle_new_token,
            trade_handler=_handle_solana_trade,
        )

        # Robinhood Chain / Pons + Noxa launchpad indexer setup
        robinhood_tasks: list[asyncio.Task] = []
        if self.settings.enable_pons_robinhood:
            state = await self.storage.get_chain_state("robinhood")
            start_block = state["last_block"] if state else None

            async def save_last_block(block: int) -> None:
                await self.storage.set_chain_state("robinhood", block)

            self._pons_indexer = PonsIndexer(
                self.robinhood, token_handler=self._handle_robinhood_token
            )
            robinhood_tasks.append(
                self._pons_indexer.start(
                    start_block=start_block,
                    save_last_block=save_last_block,
                )
            )

        if self.settings.enable_noxa_robinhood:
            noxa_state = await self.storage.get_chain_state("noxa")
            noxa_start_count = noxa_state["last_block"] if noxa_state else 0

            async def save_noxa_count(count: int) -> None:
                await self.storage.set_chain_state("noxa", count)

            self._noxa_indexer = NoxaIndexer(
                self.robinhood, token_handler=self._handle_robinhood_token
            )
            robinhood_tasks.append(
                self._noxa_indexer.start(
                    get_last_count=lambda: noxa_start_count,
                    save_last_count=save_noxa_count,
                )
            )

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
            if self._pons_indexer:
                await self._pons_indexer.stop()
            if self._noxa_indexer:
                await self._noxa_indexer.stop()
            telegram_task.cancel()
            for t in robinhood_tasks:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
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
