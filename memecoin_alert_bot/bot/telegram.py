"""Telegram delivery and command handling."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    ContextTypes,
)

from memecoin_alert_bot.bot.formatter import format_alert, should_send_alert
from memecoin_alert_bot.config import Settings
from memecoin_alert_bot.engine.models import Alert
from memecoin_alert_bot.storage.sqlite import Storage

logger = logging.getLogger(__name__)


class TelegramBot:
    """Manage Telegram app lifecycle and alert delivery."""

    def __init__(self, settings: Settings, storage: Storage):
        self.settings = settings
        self.storage = storage
        self.application: Application | None = None
        self._ready = False

    async def setup(self, retries: int = 6, backoff: float = 5.0) -> Application:
        """Build the Telegram application and register command handlers.

        Retries transient network failures (timeouts, DNS, TLS drops) so a
        momentary outage cannot crash the whole bot at startup.
        """
        builder = ApplicationBuilder().token(self.settings.telegram_bot_token)
        self.application = builder.build()
        self.application.add_handler(CommandHandler("start", self._cmd_start))
        self.application.add_handler(CommandHandler("status", self._cmd_status))
        self.application.add_handler(CommandHandler("recent", self._cmd_recent))
        self.application.add_handler(CommandHandler("test", self._cmd_test))

        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                await self.application.initialize()
                self._ready = True
                logger.info(
                    "Telegram destinations configured: %d\n",
                    len(self.settings.get_chat_ids()),
                )
                return self.application
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Telegram init failed (attempt %d/%d): %s — retrying in %.0fs",
                    attempt, retries, exc, backoff * attempt,
                )
                await asyncio.sleep(backoff * attempt)
        raise last_error  # type: ignore[misc]

    async def start(self) -> None:
        """Start polling or webhook depending on configuration."""
        if not self._ready or self.application is None:
            await self.setup()

        await self.application.start()
        if self.settings.webhook_url:
            port = int(self.settings.port)
            await self.application.updater.start_webhook(
                listen="0.0.0.0",
                port=port,
                webhook_url=self.settings.webhook_url,
            )
            logger.info("Telegram webhook listening on port %s", port)
        else:
            await self.application.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram polling started")

    async def stop(self) -> None:
        if self.application:
            try:
                if self.application.updater.running:
                    await self.application.updater.stop()
            except Exception:
                pass
            await self.application.stop()
            await self.application.shutdown()
            self._ready = False

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None:
            return
        chat_id = update.effective_chat.id
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🤖 *Memecoin Alert Bot*\n\n"
                "I monitor pump.fun and score new tokens with the SPYZER framework.\n\n"
                "Commands:\n"
                "/start - this message\n"
                "/status - bot status\n"
                "/recent - recent alerts"
            ),
            parse_mode="Markdown",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None:
            return
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ Bot is running and listening to PumpPortal.",
        )

    async def _cmd_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a test message to every configured destination chat."""
        chat_ids = self.settings.get_chat_ids()
        if update.effective_chat is not None:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f" Sending test to {len(chat_ids)} destination(s)...",
            )
        ok, fail = 0, 0
        for cid in chat_ids:
            try:
                await context.bot.send_message(
                    chat_id=cid,
                    text="🧪 *Fire Intern test* — this group is configured correctly.",
                    parse_mode="Markdown",
                )
                ok += 1
            except Exception as exc:
                fail += 1
                logger.warning("Test send to %s failed: %s", cid, exc)
        if update.effective_chat is not None:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"🧪 Done: {ok} delivered, {fail} failed.",
            )

    async def _cmd_recent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None:
            return
        rows = await self.storage.recent_alerts(limit=10)
        if not rows:
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text="No recent alerts stored."
            )
            return
        lines = ["*Recent alerts*"]
        for r in rows:
            lines.append(
                f"- {r['primary_signal']} {r['symbol']} | {r['verdict']} | {r['risk']} "
                f"({r['composite_score']:.2f})"
            )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="\n".join(lines),
            parse_mode="Markdown",
        )

    async def send_moon_update(
        self,
        symbol: str,
        multiple: float,
        mc_from: float | None,
        mc_to: float | None,
    ) -> bool:
        """Send the follow-up 'is up NX' price feedback card."""
        if not self._ready or self.application is None:
            return False

        multiple = max(multiple, 1.0)
        x_text = f"{multiple:.1f}X"

        def _money(v: float | None) -> str:
            if v is None:
                return "?"
            if v >= 1_000_000:
                s = f"{v/1_000_000:.1f}M"
            elif v >= 1_000:
                s = f"{v/1_000:.1f}K"
            else:
                s = f"{v:.0f}"
            return "$" + s.replace(".0K", "K").replace(".0M", "M")

        money = f"{_money(mc_from)} —> {_money(mc_to)} 💵" if mc_from and mc_to else ""

        text = (
            f"📈 {symbol} is up {x_text} 📈\n"
            f"from ⚡️ Fire Intern Signal\n\n"
            f"{money}\n\n"
            f"💸💸💸💸"
        ).strip()

        chat_ids = self.settings.get_chat_ids()
        sent_any = False
        for chat_id in chat_ids:
            try:
                await self.application.bot.send_message(chat_id=chat_id, text=text)
                sent_any = True
            except Exception:
                logger.exception("Failed to send moon update to %s", chat_id)
        return sent_any

    # ------------------------------------------------------------------
    # Alert delivery
    # ------------------------------------------------------------------

    async def send_alert(self, alert: Alert) -> bool:
        """Send an alert to the configured chat if it passes filters."""
        if not should_send_alert(
            alert,
            mode=self.settings.subscription_mode,
            min_confidence=self.settings.min_confidence,
        ):
            return False

        if not self._ready or self.application is None:
            logger.warning("Telegram application not ready; alert not sent")
            return False

        chat_ids = self.settings.get_chat_ids() or (
            [os.environ.get("TELEGRAM_CHAT_ID")] if os.environ.get("TELEGRAM_CHAT_ID") else []
        )
        chat_ids = [c for c in chat_ids if c]
        if not chat_ids:
            logger.warning("No TELEGRAM_CHAT_ID configured")
            return False

        text, keyboard = format_alert(alert)
        sent_any = False
        for chat_id in chat_ids:
            try:
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                sent_any = True
            except Exception:
                logger.exception("Failed to send Telegram alert to %s", chat_id)
        return sent_any
