"""Entry point for the Memecoin Alert Bot."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from typing import Any

import aiohttp

from memecoin_alert_bot.bot.telegram import TelegramBot
from memecoin_alert_bot.config import get_settings
from memecoin_alert_bot.data.bitquery import BitqueryClient
from memecoin_alert_bot.data.bubblemaps import BubblemapsClient
from memecoin_alert_bot.data.direct_discovery import DEXSCREENER_SLUG, DirectDiscoveryIndexer
from memecoin_alert_bot.data.stockyard import StockyardClient
from memecoin_alert_bot.data.gmgn import GmgnClient
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


def _to_f(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


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
        self._direct_discovery: DirectDiscoveryIndexer | None = None
        self._solana_discovery: DirectDiscoveryIndexer | None = None
        self._symbol_cooldowns: dict[str, float] = {}  # symbol -> last alert timestamp

    async def _init_clients(self) -> None:
        # DNS-resilient shared session: cached resolver with DoH fallback
        # rides through the machine's intermittent "getaddrinfo failed" drops.
        from memecoin_alert_bot.utils.resolver import CachedDohResolver

        self._resolver = CachedDohResolver()
        connector = aiohttp.TCPConnector(resolver=self._resolver)
        self.session = aiohttp.ClientSession(connector=connector)
        self.pumpfun = PumpFunClient(self.session)
        self.dexscreener = DexScreenerClient(self.session)
        self.rugcheck = RugcheckClient(self.settings.rugcheck_api_key, self.session)
        self.solscan = SolscanClient(self.settings.solscan_api_key, self.session)
        self.x_api = XApiClient(self.settings.x_bearer_token, self.session)
        self.robinhood = RobinhoodChainClient(self.settings.robinhood_rpc_url, self.session)
        self.bubblemaps = BubblemapsClient(self.settings.bubblemaps_api_key, self.session)
        self.bitquery = BitqueryClient(self.settings.bitquery_api_key, self.session)
        self.gmgn = GmgnClient(self.settings.gmgn_api_key, self.session)
        self.stockyard = StockyardClient(self.session)
        self._clients = [
            self.pumpfun,
            self.dexscreener,
            self.rugcheck,
            self.solscan,
            self.x_api,
            self.robinhood,
            self.bubblemaps,
            self.bitquery,
            self.gmgn,
            self.stockyard,
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
        if getattr(self, "_resolver", None):
            await self._resolver.close()

    async def _enrich_solana_coin(self, coin) -> None:
        """Fetch and merge metadata from all configured Solana sources."""
        from memecoin_alert_bot.utils.helpers import is_valid_api_key

        async with self._solana_semaphore:
            mint = coin.mint

            # Bitquery: primary data source for volume, price, buy pressure
            tasks = []
            bitquery_task = None
            if is_valid_api_key(self.settings.bitquery_api_key):
                bitquery_task = asyncio.create_task(self.bitquery.enrich_coin(mint))
                tasks.append(bitquery_task)

            # DexScreener and pump.fun as fallbacks (always run alongside)
            if self.settings.enable_pumpfun_rest:
                tasks.append(self.pumpfun.enrich_coin(mint, {}))
            if self.settings.enable_dexscreener:
                tasks.append(self.dexscreener.enrich_coin(mint, {}))

            # Safety / cluster — Rugcheck is free/keyless, always run it.
            tasks.append(self.rugcheck.enrich_coin(mint, {}))
            if self.gmgn.enabled:
                tasks.append(self.gmgn.enrich_coin(mint, "solana"))
            if is_valid_api_key(self.settings.solscan_api_key):
                tasks.append(self.solscan.enrich_coin(mint, {}))
            if is_valid_api_key(self.settings.bubblemaps_api_key):
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

    @staticmethod
    def _identity_incomplete(coin: CoinData) -> bool:
        return not coin.symbol or coin.symbol == "UNKNOWN" or not coin.name

    async def _fill_metadata_from_uri(self, coin: CoinData) -> None:
        """Fill name/symbol from the create event's Metaplex metadata JSON.

        PumpPortal events carry an IPFS ``uri``; on-chain scanners show the
        name from this same document, so it is the authoritative fallback
        when REST enrichment times out.
        """
        if not coin.metadata_uri or self.session is None or self.session.closed:
            return
        from memecoin_alert_bot.utils.helpers import fetch_metadata_json

        meta = await fetch_metadata_json(self.session, coin.metadata_uri)
        if not meta:
            return
        if meta.get("name") and not coin.name:
            coin.name = meta["name"]
        if meta.get("symbol") and (not coin.symbol or coin.symbol == "UNKNOWN"):
            coin.symbol = meta["symbol"]
        if meta.get("description") and not coin.description:
            coin.description = meta["description"]

    async def _handle_new_token(self, event: dict[str, Any]) -> None:
        """Process a new Solana token event end-to-end."""
        mint = event.get("mint") or event.get("token")
        if not mint:
            return

        try:
            coin = normalizer.create_from_pumpportal(event, sol_usd=self.settings.sol_usd)
            coin = await self._enrich_solana_coin(coin)

            # Identity rescue: the scanner shows a name even when our REST
            # enrichment times out, so pull it from the event's metadata URI
            # and retry enrichment once before ever displaying UNKNOWN.
            if self._identity_incomplete(coin):
                await self._fill_metadata_from_uri(coin)
            if self._identity_incomplete(coin):
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

    async def _enrich_robinhood_coin(self, coin: CoinData) -> CoinData:
        """DexScreener/GMGN fallback for Robinhood tokens lacking price/MC data.

        Covers Uniswap v4 launches (non-callable pool IDs) and any token the
        V3-only RPC path could not price. Verified USD windows upgrade the
        directional-only flow data via normalizer precedence.
        """
        if coin.price is not None and coin.market_cap is not None:
            return coin
        try:
            enrichment = await self.dexscreener.enrich_coin(coin.mint, {}, chain="robinhood")
            if enrichment.get("sources", {}).get("dexscreener"):
                coin = normalizer.merge_enrichment(coin, enrichment)
        except Exception as exc:
            logger.debug("Robinhood Dex fallback failed for %s: %s", coin.mint, exc)
        if self.gmgn.enabled:
            try:
                enrichment = await self.gmgn.enrich_coin(coin.mint, "robinhood")
                if enrichment.get("sources", {}).get("gmgn"):
                    coin = normalizer.merge_enrichment(coin, enrichment)
            except Exception as exc:
                logger.debug("Robinhood GMGN enrichment failed for %s: %s", coin.mint, exc)
        return coin

    async def _handle_direct_solana_token(self, coin: CoinData) -> None:
        """Solana fallback discovery (DexScreener feed) — PumpPortal-independent."""
        try:
            coin = await self._enrich_solana_coin(coin)

            trades = self._solana_trades.get(coin.mint)
            if trades:
                total = trades.get("buy", 0) + trades.get("sell", 0)
                if total > 0:
                    coin.buy_pressure = trades.get("buy", 0) / total
                    if coin.volume_24h is None:
                        coin.volume_24h = total * 50
            await self._evaluate_and_alert(coin)
        except Exception:
            logger.exception("Failed to process direct Solana token %s", coin.mint)

    async def _handle_robinhood_token(self, coin: CoinData) -> None:
        """Process a Robinhood Chain token discovered by Pons, Noxa, or direct discovery."""
        try:
            coin = await self._enrich_robinhood_coin(coin)
            await self._evaluate_and_alert(coin)
        except Exception:
            logger.exception("Failed to process Robinhood token %s", coin.mint)

    async def _record_decision(
        self,
        coin: CoinData,
        stage: str,
        reason: str = "",
        score_json: str | None = None,
    ) -> None:
        """Ledger every evaluation decision (guide §5)."""
        try:
            await self.storage.record_decision(
                mint=coin.mint,
                chain=coin.chain,
                symbol=coin.symbol,
                stage=stage,
                reason=reason,
                market_cap=coin.market_cap,
                score_json=score_json,
            )
        except Exception:
            logger.debug("Decision record failed for %s", coin.mint)

    async def _evaluate_and_alert(self, coin: CoinData) -> str:
        """Run detectors, score, and optionally send a Telegram alert.

        Returns the decision stage so callers (e.g. the Solana rescan loop)
        know whether to keep watching this token.
        """
        mint = coin.mint

        # ── Chain switch: Solana can be silenced without touching Robinhood ──
        if coin.chain == "solana" and not self.settings.enable_solana_alerts:
            logger.debug("Skipping %s — Solana alerts disabled", mint)
            await self._record_decision(coin, "solana_disabled")
            return "solana_disabled"

        # ── Identity filter: never alert on unnamed/UNKNOWN tokens ──
        # This happens when enrichment failed (network timeouts) and means
        # we know almost nothing reliable about the token.
        if not coin.symbol or coin.symbol == "UNKNOWN":
            logger.debug("Skipping %s — symbol unknown (enrichment incomplete)", mint)
            await self._record_decision(coin, "unknown_identity", "symbol UNKNOWN")
            return "unknown_identity"

        # ── Market-cap filter (strict, USD) ──
        if coin.market_cap is None:
            logger.debug("Skipping %s — no market cap data", mint)
            await self._record_decision(coin, "mc_missing", "no market cap data")
            return "mc_missing"

        if coin.market_cap < self.settings.min_market_cap:
            logger.debug(
                "Skipping %s — MC $%.0f < min $%.0f",
                mint, coin.market_cap, self.settings.min_market_cap,
            )
            await self._record_decision(coin, "mc_below_floor", f"mc {coin.market_cap:.0f}")
            # pump.fun tokens are BORN around $5k; watch for an hour and
            # re-evaluate the moment they cross the floor with real buying.
            if coin.chain == "solana":
                try:
                    await self.storage.add_sol_watchlist(
                        mint=mint,
                        symbol=coin.symbol,
                        name=coin.name,
                        metadata_uri=coin.metadata_uri,
                        first_mc=coin.market_cap,
                    )
                except Exception:
                    logger.debug("Watchlist add failed for %s", mint)
            return "mc_below_floor"

        if self.settings.max_market_cap > 0 and coin.market_cap > self.settings.max_market_cap:
            logger.debug(
                "Skipping %s — MC $%.0f > max $%.0f",
                mint, coin.market_cap, self.settings.max_market_cap,
            )
            await self._record_decision(coin, "mc_above_ceiling", f"mc {coin.market_cap:.0f}")
            return "mc_above_ceiling"

        # ── NLP narrative analysis (must run before buying-activity check) ──
        narrative_text = f"{coin.name} {coin.description}"
        na = get_narrative_analyzer()
        coin.narrative_keywords = na.extract_keywords(narrative_text)[:10]
        coin.narrative_strength = na.narrative_strength(narrative_text)
        coin.vamp_similarity = na.check_vamp_risk(narrative_text)
        na.add_token(coin.mint, narrative_text)

        # ── Buying-activity filter (strict — ONLY coins people are buying) ──
        # Verified USD volume OR verified buy/sell transaction counts.
        # Directional-only chain data must show actual buy transactions.
        has_verified_volume = (
            (coin.volume_24h is not None and coin.volume_24h > 0)
            or (coin.buy_volume_1h is not None and coin.buy_volume_1h > 0)
            or (coin.volume_1h is not None and coin.volume_1h > 0)
        )
        has_directional_buys = (
            coin.flow_data_quality == "directional_only"
            and ((coin.buys_5m or 0) + (coin.buys_1h or 0)) > 0
        )
        if not has_verified_volume and not has_directional_buys:
            logger.debug("Skipping %s — no buying activity detected", mint)
            await self._record_decision(coin, "no_buying_activity")
            return "no_buying_activity"

        # ── Per-symbol cooldown (catch name copycats) ──
        import time
        now = time.time()
        last = self._symbol_cooldowns.get(coin.symbol)
        if last and (now - last) < 60:
            logger.debug("Skipping %s — symbol %s on cooldown", mint, coin.symbol)
            await self._record_decision(coin, "symbol_cooldown", coin.symbol)
            return "symbol_cooldown"
        self._symbol_cooldowns[coin.symbol] = now

        await self.storage.upsert_coin(coin)

        signals = detectors.run_all(coin)
        score = scorer.score_coin(coin, signals)

        alert = Alert(coin=coin, signals=signals, score=score)

        if await self.storage.is_on_cooldown(mint, self.settings.alert_cooldown_seconds):
            logger.debug("Alert for %s is on cooldown", mint)
            await self._record_decision(coin, "alert_cooldown")
            return "alert_cooldown"

        await self.storage.record_backtest_snapshot(coin, score)
        alert_id = await self.storage.save_alert(alert)

        # Ledger the delivery decision with its actual suppression reason.
        from memecoin_alert_bot.bot.formatter import should_send_alert

        send_ok = should_send_alert(
            alert,
            mode=self.settings.subscription_mode,
            min_confidence=self.settings.min_confidence,
        )
        if not send_ok:
            if score.tier.value == "HIGH_RISK":
                await self._record_decision(coin, "suppressed_high_risk", score_json=score.model_dump_json())
                return "suppressed_high_risk"
            elif score.confidence < self.settings.min_confidence:
                await self._record_decision(coin, "suppressed_confidence", score_json=score.model_dump_json())
                return "suppressed_confidence"
            else:
                await self._record_decision(coin, "suppressed_pass", score_json=score.model_dump_json())
                return "suppressed_pass"

        sent = await self.telegram.send_alert(alert, alert_id=alert_id)
        if sent:
            await self._record_decision(coin, "sent", score_json=score.model_dump_json())
            await self.storage.set_cooldown(mint)
            logger.info(
                "Alert sent: %s (%s on %s) | %s | score=%.2f",
                coin.symbol,
                mint,
                coin.chain,
                score.verdict.value,
                score.composite_score,
            )
            return "sent"
        elif not send_ok:
            logger.debug(
                "Alert suppressed: %s | tier=%s confidence=%.2f",
                coin.symbol, score.tier.value, score.confidence,
            )
        return "delivery_failed"

    async def _solana_rescan_loop(self, interval_seconds: float = 45.0) -> None:
        """Re-check watched Solana launches; alert when they cross the floor.

        pump.fun tokens are born below the market-cap floor and grow into it
        within minutes when people buy. Without this loop they are evaluated
        once at birth and permanently missed.
        """
        logger.info("Solana rescan loop started (watchlist re-check every %.0fs)", interval_seconds)
        while True:
            try:
                rows = await self.storage.get_sol_watchlist(limit=120)
                for row in rows:
                    mint = row["mint"]
                    try:
                        enrichment = await self.dexscreener.enrich_coin(
                            mint, {}, chain="solana"
                        )
                        mc = enrichment.get("market_cap")
                        if mc is None or mc < self.settings.min_market_cap:
                            continue  # still below floor — keep watching

                        logger.info(
                            "Rescan: %s crossed the floor ($%.0f) — re-evaluating",
                            row["symbol"] or mint, mc,
                        )
                        coin = CoinData(
                            mint=mint,
                            chain="solana",
                            symbol=row["symbol"] or "UNKNOWN",
                            name=row["name"] or "",
                            metadata_uri=row["metadata_uri"],
                            age_seconds=None,
                            sources={"sol_rescan": {"first_mc": row["first_mc"]}},
                        )
                        coin = normalizer.merge_enrichment(coin, enrichment)

                        if self._identity_incomplete(coin):
                            await self._fill_metadata_from_uri(coin)
                        if self._identity_incomplete(coin):
                            coin = await self._enrich_solana_coin(coin)

                        trades = self._solana_trades.get(mint)
                        if trades:
                            total = trades.get("buy", 0) + trades.get("sell", 0)
                            if total > 0:
                                coin.buy_pressure = trades.get("buy", 0) / total

                        stage = await self._evaluate_and_alert(coin)
                        # Stop watching once fully processed, or keep watching
                        # while it still has no market/volume data.
                        if stage not in ("mc_missing", "no_buying_activity"):
                            await self.storage.remove_sol_watchlist(mint)
                    except Exception as exc:
                        logger.debug("Rescan failed for %s: %s", mint, exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Solana rescan loop error")
            await asyncio.sleep(interval_seconds)

    async def _moon_check(
        self,
        alert_id: int,
        mint: str,
        chain: str,
        symbol: str,
        mc_alert: float | None,
        price_alert: float | None,
    ) -> bool:
        """Check one token's live multiple against the ladder; send if crossed.

        Cumulative multiple is always measured from the original call's
        baseline (moon_state) — dumps never reset it. Returns True when an
        update was sent.
        """
        enrichment = await self.dexscreener.enrich_coin(mint, {}, chain=chain)
        mc_now = enrichment.get("market_cap")
        price_now = enrichment.get("price")
        if mc_now is None and price_now is None:
            return False

        state = await self.storage.ensure_moon_state(mint, mc_alert, price_alert)
        baseline_mc = state.get("baseline_mc")
        baseline_price = state.get("baseline_price")
        last_multiple = float(state.get("last_multiple") or 1.0)

        cumulative = None
        if baseline_mc and mc_now and baseline_mc > 0:
            cumulative = mc_now / baseline_mc
        elif baseline_price and price_now and baseline_price > 0:
            cumulative = price_now / baseline_price
        if cumulative is None:
            return False

        from memecoin_alert_bot.utils.helpers import next_moon_threshold

        threshold = next_moon_threshold(self.settings.moon_update_pct, last_multiple)
        if cumulative < threshold or cumulative <= last_multiple:
            return False

        mc_from = baseline_mc if (baseline_mc and mc_now) else None
        mc_to = mc_now if (baseline_mc and mc_now) else None
        sent = await self.telegram.send_moon_update(
            symbol, cumulative, mc_from, mc_to, alert_id=alert_id
        )
        await self.storage.set_moon_multiple(mint, cumulative)
        if sent:
            coin_like = CoinData(mint=mint, chain=chain, symbol=symbol, market_cap=mc_now)
            await self._record_decision(
                coin_like, "moon_update", f"{cumulative:.2f}X cumulative (live)"
            )
            logger.info("Moon update sent: %s up %.2fX cumulative", symbol, cumulative)
        return sent

    async def _moon_watch_loop(self, interval_seconds: float = 30.0, window_minutes: int = 30) -> None:
        """Fast loop: watch newly alerted tokens every 30s for 30 minutes.

        Fixed horizons (+5m/+15m/...) are too slow for memecoin pumps — a
        14X can happen and retrace between snapshots. This loop samples the
        live price continuously during the critical first half hour so the
        ladder fires the moment it is crossed.
        """
        logger.info(
            "Moon watch started (every %.0fs for %dm after each alert)",
            interval_seconds, window_minutes,
        )
        while True:
            try:
                rows = await self.storage.get_recent_alerts_for_moon(window_minutes)
                for row in rows:
                    chain, symbol = "solana", "TOKEN"
                    mc_alert = price_alert = None
                    try:
                        payload = json.loads(row["payload"]) if row["payload"] else {}
                        coin_payload = payload.get("coin", {})
                        chain = coin_payload.get("chain", "solana")
                        symbol = coin_payload.get("symbol") or symbol
                        mc_alert = coin_payload.get("market_cap")
                        price_alert = coin_payload.get("price")
                    except Exception:
                        pass
                    try:
                        await self._moon_check(
                            alert_id=row["id"],
                            mint=row["mint"],
                            chain=chain,
                            symbol=symbol,
                            mc_alert=mc_alert,
                            price_alert=price_alert,
                        )
                    except Exception as exc:
                        logger.debug("Moon check failed for %s: %s", row["mint"], exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Moon watch loop error")
            await asyncio.sleep(interval_seconds)

    async def _gmgn_discovery_loop(self, interval_seconds: float = 30.0) -> None:
        """Discover new tokens via GMGN Trenches (sol + robinhood).

        Higher-signal than the DexScreener profile feed: launchpad filters,
        dev-holdings data, and pre-graduation tokens (pump.fun bonding curve)
        appear here the moment they are created.
        """
        logger.info("GMGN Trenches discovery started")
        while True:
            try:
                if not self.gmgn.enabled:
                    await asyncio.sleep(interval_seconds)
                    continue
                # Solana: pump.fun launches
                try:
                    sol = await self.gmgn.get_trenches(
                        "solana", types=["new_creation"], platforms=["Pump.fun"], limit=40
                    )
                    for item in sol.get("new_creation", []):
                        await self._process_gmgn_trench_item(item, "solana")
                except Exception as exc:
                    logger.debug("GMGN sol trenches failed: %s", exc)

                # Robinhood: all new creations (native chain support!)
                try:
                    rh = await self.gmgn.get_trenches(
                        "robinhood", types=["new_creation"], limit=40
                    )
                    for item in rh.get("new_creation", []):
                        await self._process_gmgn_trench_item(item, "robinhood")
                except Exception as exc:
                    logger.debug("GMGN robinhood trenches failed: %s", exc)

                # Robinhood trending (1m): sweeps up non-Pons tokens that
                # already have live activity but were missed at creation —
                # only tokens never processed before are evaluated.
                try:
                    trending = await self.gmgn.get_trending(
                        "robinhood", interval="1m", limit=50, order_by="swaps"
                    )
                    for item in trending:
                        mint = item.get("address") or item.get("token_address") or ""
                        if not mint:
                            continue
                        if await self.storage.get_coin(mint):
                            continue  # already processed previously
                        logger.info(
                            "GMGN trending: unseen robinhood token %s — evaluating",
                            item.get("symbol") or mint,
                        )
                        await self._process_gmgn_trench_item(item, "robinhood")
                except Exception as exc:
                    logger.debug("GMGN robinhood trending failed: %s", exc)

                # DexScreener sweep: fresh robinhood pairs via search —
                # GMGN and Dex jointly cover non-Pons launches.
                try:
                    fresh = await self.dexscreener.get_new_pairs(
                        DEXSCREENER_SLUG, max_age_minutes=60
                    )
                    for pair in fresh:
                        base = pair.get("baseToken") or {}
                        mint = base.get("address") or ""
                        if not mint or await self.storage.get_coin(mint):
                            continue
                        best = (
                            await self._direct_discovery._best_pair(mint)
                            if self._direct_discovery
                            else None
                        )
                        if not best:
                            continue
                        coin = await self._direct_discovery._build_coin(mint, best)
                        if coin:
                            logger.info(
                                "Dex sweep: unseen robinhood token %s — evaluating",
                                coin.symbol,
                            )
                            await self._handle_robinhood_token(coin)
                except Exception as exc:
                    logger.debug("Dex robinhood sweep failed: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("GMGN discovery loop error")
            await asyncio.sleep(interval_seconds)

    async def _process_gmgn_trench_item(self, item: dict[str, Any], chain: str) -> None:
        """Convert a Trenches RankItem into the standard pipeline."""
        from memecoin_alert_bot.engine.normalizer import merge_enrichment

        mint = item.get("address") or item.get("token_address") or ""
        if not mint:
            return
        created = item.get("creation_timestamp")
        age_seconds = None
        if created:
            try:
                age_seconds = max(0, int(import_time() - float(created)))
            except (TypeError, ValueError):
                pass

        coin = CoinData(
            mint=mint,
            chain=chain,
            symbol=item.get("symbol") or "UNKNOWN",
            name=item.get("name") or "",
            market_cap=_to_f(item.get("market_cap")),
            price=_to_f(item.get("price")),
            age_seconds=age_seconds,
            sources={"gmgn_trenches": item},
        )
        # Route through the standard handlers (dedup via mint cooldown).
        if chain == "solana":
            await self._handle_direct_solana_token(coin)
        else:
            await self._handle_robinhood_token(coin)

    async def _stockyard_discovery_loop(self, interval_seconds: float = 60.0) -> None:
        """Discover stock-paired memecoins via the StockYard map feed.

        Robinhood memecoins can trade paired against tokenized stocks (NVDA,
        COST...). StockYard publishes the whole stock->memecoin graph with
        live liquidity/volume — this loop evaluates never-seen pairs.
        """
        logger.info("StockYard discovery started (stock-pair memecoins)")
        while True:
            try:
                memes = await self.stockyard.get_paired_memecoins()
                seen = 0
                for meme in memes:
                    mint = meme["mint"]
                    if await self.storage.get_coin(mint):
                        seen += 1
                        continue
                    try:
                        pair_id = meme.get("pool_id") or ""
                        coin = CoinData(
                            mint=mint,
                            chain="robinhood",
                            chain_id=4663,
                            symbol=meme.get("symbol") or "UNKNOWN",
                            name=meme.get("name") or "",
                            market_cap=meme.get("market_cap"),
                            liquidity=meme.get("liquidity"),
                            volume_24h=meme.get("volume_24h"),
                            age_seconds=(
                                int(float(meme["age_hours"]) * 3600)
                                if meme.get("age_hours") is not None
                                else None
                            ),
                            pool_address=pair_id if len(pair_id) == 42 else None,
                            sources={
                                "stockyard": {
                                    "stock_ticker": meme.get("stock_ticker"),
                                    "launchpad": meme.get("launchpad"),
                                    "tx_count": meme.get("tx_count"),
                                }
                            },
                        )
                        logger.info(
                            "StockYard: unseen pair %s (%s on %s) — evaluating",
                            coin.symbol,
                            meme.get("stock_ticker"),
                            meme.get("launchpad"),
                        )
                        await self._handle_robinhood_token(coin)
                    except Exception as exc:
                        logger.debug("StockYard token %s failed: %s", mint, exc)
                logger.debug("StockYard pass: %d pairs, %d already seen", len(memes), seen)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("StockYard discovery loop error")
            await asyncio.sleep(interval_seconds)

    async def _outcome_tracker_loop(self, interval_seconds: float = 60.0) -> None:
        """Record calibration outcomes at +5m/+15m/+1h/+24h (guide §5).

        Moon updates are sent by _moon_watch_loop (continuous); this loop
        only records the fixed-horizon snapshots for calibration.
        """
        horizons = [5, 15, 60, 1440]
        logger.info("Outcome tracker started (horizons: %s)", horizons)
        while True:
            try:
                due = await self.storage.get_alerts_without_outcomes(horizons)
                for item in due:
                    mint = item["mint"]
                    chain = "solana"
                    price_alert = None
                    mc_alert = None
                    try:
                        payload = json.loads(item["payload"]) if item["payload"] else {}
                        coin_payload = payload.get("coin", {})
                        chain = coin_payload.get("chain", "solana")
                        price_alert = coin_payload.get("price")
                        mc_alert = coin_payload.get("market_cap")
                    except Exception:
                        pass

                    enrichment = await self.dexscreener.enrich_coin(
                        mint, {}, chain=chain
                    )
                    price_now = enrichment.get("price")
                    mc_now = enrichment.get("market_cap")
                    if price_now is None and mc_now is None:
                        # No market data (yet) — retry on a later pass.
                        continue

                    await self.storage.record_outcome(
                        alert_id=item["alert_id"],
                        mint=mint,
                        horizon_min=item["horizon_min"],
                        alert_at=item["generated_at"],
                        price_alert=price_alert,
                        price_horizon=price_now,
                        mc_alert=mc_alert,
                        mc_horizon=mc_now,
                    )
                    pct = None
                    if price_alert and price_now and price_alert > 0:
                        pct = (price_now / price_alert - 1) * 100
                    logger.info(
                        "Outcome: %s +%sm %+.1f%%", mint, item["horizon_min"], pct or 0.0
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Outcome tracker error")
            await asyncio.sleep(interval_seconds)

    async def run(self) -> None:
        """Start the bot and run until shutdown."""
        await self.storage.connect()
        await self._init_clients()

        # Telegram setup
        await self.telegram.setup()
        telegram_task = asyncio.create_task(self.telegram.start())

        # PumpPortal listener setup (tokens + trades for Solana buying pressure)
        self._solana_trades: dict[str, dict[str, int]] = {}
        pumpportal = None

        async def _handle_solana_trade(data: dict[str, Any]) -> None:
            mint = data.get("mint") or data.get("token")
            tx_type = data.get("txType", "")
            if mint and tx_type in ("buy", "sell"):
                tracker = self._solana_trades.setdefault(mint, {"buy": 0, "sell": 0})
                tracker[tx_type] = tracker.get(tx_type, 0) + 1

        if self.settings.enable_solana_alerts:
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

        # Direct ERC-20 / new-pool discovery (catches non-factory launches
        # such as direct deployments and Uniswap v4 pairs).
        if self.settings.enable_direct_discovery:
            self._direct_discovery = DirectDiscoveryIndexer(
                self.dexscreener,
                self.robinhood,
                token_handler=self._handle_robinhood_token,
            )
            robinhood_tasks.append(self._direct_discovery.start())

            # Solana fallback discovery via the same DexScreener feed —
            # keeps working when the PumpPortal WebSocket is unreachable.
            if self.settings.enable_solana_alerts:
                self._solana_discovery = DirectDiscoveryIndexer(
                    self.dexscreener,
                    None,
                    token_handler=self._handle_direct_solana_token,
                    chain_slug="solana",
                    chain_name="solana",
                )
                robinhood_tasks.append(self._solana_discovery.start(interval_seconds=15.0))

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
            except NotImplementedError:
                pass  # Windows does not support add_signal_handler for SIGTERM

        logger.info("Starting Memecoin Alert Bot")
        pumpportal_task = pumpportal.start() if pumpportal else None
        outcome_task = asyncio.create_task(self._outcome_tracker_loop())
        rescan_task = (
            asyncio.create_task(self._solana_rescan_loop())
            if self.settings.enable_solana_alerts
            else None
        )
        moon_watch_task = asyncio.create_task(self._moon_watch_loop())
        gmgn_discovery_task = asyncio.create_task(self._gmgn_discovery_loop())
        stockyard_task = (
            asyncio.create_task(self._stockyard_discovery_loop())
            if self.settings.enable_stockyard
            else None
        )
        if not self.settings.enable_solana_alerts:
            logger.info("Solana alerts DISABLED (ENABLE_SOLANA_ALERTS=false) — Robinhood only")

        try:
            await self._shutdown_event.wait()
        finally:
            logger.info("Shutting down...")
            if pumpportal:
                await pumpportal.stop()
            if self._pons_indexer:
                await self._pons_indexer.stop()
            if self._noxa_indexer:
                await self._noxa_indexer.stop()
            if self._direct_discovery:
                await self._direct_discovery.stop()
            if self._solana_discovery:
                await self._solana_discovery.stop()
            telegram_task.cancel()
            outcome_task.cancel()
            rescan_task.cancel()
            moon_watch_task.cancel()
            gmgn_discovery_task.cancel()
            if stockyard_task:
                stockyard_task.cancel()
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
            try:
                await outcome_task
            except asyncio.CancelledError:
                pass
            if rescan_task:
                try:
                    await rescan_task
                except asyncio.CancelledError:
                    pass
            try:
                await moon_watch_task
            except asyncio.CancelledError:
                pass
            try:
                await gmgn_discovery_task
            except asyncio.CancelledError:
                pass
            if stockyard_task:
                try:
                    await stockyard_task
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
