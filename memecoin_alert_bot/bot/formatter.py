"""Format alerts into rich Telegram messages."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from memecoin_alert_bot.engine.models import Alert
from memecoin_alert_bot.utils.helpers import format_currency


def _format_holders(holders: int | None) -> str:
    return f"{holders:,}" if holders is not None else "N/A"


def format_alert(alert: Alert) -> tuple[str, InlineKeyboardMarkup]:
    """Build Markdown text and inline keyboard for an alert."""
    coin = alert.coin
    score = alert.score

    verdict_emoji = {
        "BUY": "✅",
        "WAIT": "⏳",
        "DYOR": "⚠️",
        "PASS": "❌",
    }.get(score.verdict.value, "ℹ️")

    safety_lines = []
    lp = coin.safety.lp_locked
    safety_lines.append("  • LP Locked: ✅" if lp is True else ("  • LP Locked: ❌" if lp is False else "  • LP Locked: ❓"))

    mint_auth = coin.safety.mint_authority_enabled
    safety_lines.append(
        "  • Mint Auth: ✅ Disabled"
        if mint_auth is False
        else ("  • Mint Auth: ❌ Enabled" if mint_auth is True else "  • Mint Auth: ❓")
    )
    safety_lines.append(f"  • Top Holder: {coin.top_holder_pct:.2f}%")

    why_lines = []
    for sig in alert.signals:
        for reason in sig.reasons[:2]:
            why_lines.append(f"  • {sig.signal_type.emoji} {reason}")
    if not why_lines:
        why_lines.append("  • No specific trigger matched")

    lines = [
        "════════════════════════════════════",
        f"{alert.primary_signal} ALERT",
        "════════════════════════════════════",
        "",
        f"📌 {coin.name} (${coin.symbol})",
        f"🔗 CA: `{coin.mint}`",
        "",
        f"💰 Market Cap: {format_currency(coin.market_cap)}",
        f"📊 Volume 24h: {format_currency(coin.volume_24h)}",
        f"👥 Holders: {_format_holders(coin.holders)}",
        "",
        "🔐 Safety:",
    ]
    lines.extend(safety_lines)
    lines.extend([
        "",
        f"🎯 VERDICT: {verdict_emoji} {score.verdict.value}",
        f"⚠️ RISK: {score.risk.emoji} {score.risk.value}",
        f"📊 Confidence: {int(score.confidence * 100)}%",
        "",
        "✅ Why triggered:",
    ])
    lines.extend(why_lines)
    lines.extend([
        "",
        "⚠️ NFA | DYOR | Trade Responsibly",
        "════════════════════════════════════",
    ])

    keyboard = [
        [
            InlineKeyboardButton("🚀 Buy on pump.fun", url=coin.pump_fun_url),
            InlineKeyboardButton("📊 Chart", url=coin.dexscreener_url),
        ]
    ]
    if coin.social_links.get("twitter"):
        keyboard[0].append(InlineKeyboardButton("🐦 Twitter", url=coin.social_links["twitter"]))

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def should_send_alert(alert: Alert, mode: str = "all", min_confidence: float = 0.2) -> bool:
    """Filter alert by subscription mode and confidence."""
    if alert.score.confidence < min_confidence:
        return False
    if mode == "high":
        return alert.score.verdict.value == "BUY" or alert.score.confidence >= 0.7
    # Even PASS alerts are suppressed unless they are risk warnings; risk overrides already happen in scoring.
    return True
