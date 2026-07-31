"""Format alerts into compact Telegram messages matching the Fire Intern style."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from memecoin_alert_bot.engine.models import Alert, RiskLevel, Verdict
from memecoin_alert_bot.utils.helpers import format_currency, shorten_address

# ── Emoji & colour helpers ───────────────────────────────────────────────

VERDICT_EMOJI = {
    Verdict.BUY: "✅",
    Verdict.WAIT: "⏳",
    Verdict.DYOR: "⚠️",
    Verdict.PASS: "❌",
}

RISK_EMOJI = {
    RiskLevel.LOW: "🟢",
    RiskLevel.MEDIUM: "🟡",
    RiskLevel.HIGH: "🟠",
    RiskLevel.EXTREME: "🔴",
}

CHAIN_BADGE = {
    "solana": "☀️",
    "robinhood": "🟣",
}


def _fmt_age(seconds: int | None) -> str:
    """Compact age like '19m', '2h', '3d'."""
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _fmt_price(value: float | None) -> str:
    """Format tiny token prices compactly (0.0(5)2685 style)."""
    if value is None or value == 0:
        return "N/A"
    if value >= 1:
        return f"{value:.4f}"
    # Count leading zeros after the decimal point.
    s = f"{value:.20f}".rstrip("0")
    leading_zeros = 0
    for ch in s[2:]:  # skip "0."
        if ch == "0":
            leading_zeros += 1
        else:
            break
    if leading_zeros == 0:
        return f"{value:.4f}"
    significant = s[2 + leading_zeros: 2 + leading_zeros + 4]
    return f"0.0({leading_zeros}){significant}"


def _lp_ratio(coin) -> str:
    """Approximate LP ratio as a percentage string."""
    if coin.liquidity and coin.market_cap and coin.market_cap > 0:
        ratio = coin.liquidity / coin.market_cap * 100
        return f"{ratio:.2f}%"
    if coin.safety.lp_locked is True:
        return "Locked"
    return "N/A"


def _audit_bars(risk: RiskLevel) -> str:
    """Risk meter as coloured squares like 🟧🟧 (matches the example)."""
    count = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3, RiskLevel.EXTREME: 4}.get(risk, 2)
    return "🟧" * count + "🟩" * (4 - count)


def _short_addr(addr: str) -> str:
    """0x1234...5678 form."""
    if not addr:
        return "N/A"
    return shorten_address(addr, 4)


# ── Main formatter ────────────────────────────────────────────────────────


def format_alert(alert: Alert) -> tuple[str, InlineKeyboardMarkup]:
    """Build compact Markdown text and inline keyboard for an alert."""
    coin = alert.coin
    score = alert.score

    chain_badge = CHAIN_BADGE.get(coin.chain, "🌐")
    v_emoji = VERDICT_EMOJI.get(score.verdict, "ℹ️")
    r_emoji = RISK_EMOJI.get(score.risk, "🟢")

    # Buy / sell counts (best effort).
    buys = int(coin.buy_volume_1h or 0)
    sells = int(coin.sell_volume_1h or 0)
    total_bs = buys + sells
    buy_pct = int((buys / total_bs * 100)) if total_bs > 0 else 50

    lines: list[str] = []

    # Header
    lines.append(f"🚨 NEW Fire Intern CALL ⦿")
    lines.append(f"🔍 {coin.name} (${coin.symbol})")
    lines.append(f"➰ {coin.name} (${coin.symbol})")
    lines.append(
        f"➰{r_emoji} 🌱{_fmt_age(coin.age_seconds)} 👀{coin.holders or 0}"
    )
    lines.append("")

    # Token stats
    lines.append("📊 Token Stats")
    lines.append(f"➰ MC:   {format_currency(coin.market_cap)}")
    lines.append(f"➰ ATH:  {format_currency(coin.market_cap)}")  # no ATH tracking yet
    lines.append(f"➰ USD:  {_fmt_price(coin.price)}")
    lines.append(f"➰ LIQ:  {format_currency(coin.liquidity)}")
    lines.append(f"➰ VOL:  {format_currency(coin.volume_24h)} (24h)")
    lines.append(f"➰ 1H:   B {buys} / S {sells} ({buy_pct}%)")
    lines.append(f"➰ HLD:  {coin.holders or 'N/A'}")
    lines.append(f"➰ P:    {_short_addr(coin.mint)} 🦄")
    lines.append(f"➰ DEV:  {_short_addr(coin.dev_wallet or coin.deployer or '')}")
    lines.append("")

    # Socials
    lines.append("🔗 Socials")
    social_parts = []
    if coin.website or coin.social_links.get("website"):
        social_parts.append("Web")
    if coin.twitter or coin.social_links.get("twitter"):
        social_parts.append("𝕏")
    if coin.telegram or coin.social_links.get("telegram"):
        social_parts.append("TG")
    social_parts.append("About")
    lines.append(f"➰ {' • '.join(social_parts)}")
    lines.append("")

    # Audit
    lines.append(f"⚠️ Audit {_audit_bars(score.risk)}")
    lines.append(f"❌ LP Ratio [{_lp_ratio(coin)}]")
    mint_auth = coin.safety.mint_authority_enabled
    mint_status = "ENABLED" if mint_auth is True else ("disabled" if mint_auth is False else "unknown")
    lines.append(f"❌ Mint [{mint_status}]")
    lines.append("")

    # Verdict / risk / confidence
    lines.append(f"🎯 VERDICT: {v_emoji} {score.verdict.value}")
    lines.append(f"⚠️ RISK: {r_emoji} {score.risk.value}")
    lines.append(f"📊 Confidence: {int(score.confidence * 100)}%")
    lines.append(f"{chain_badge} Chain: {coin.chain.title()}")
    lines.append("")

    # Why triggered
    why = []
    for sig in alert.signals:
        for reason in sig.reasons[:2]:
            why.append(f"➰ {sig.signal_type.emoji} {reason}")
    if why:
        lines.append("✅ Why triggered:")
        lines.extend(why)
        lines.append("")

    lines.append("⚠️ NFA | DYOR | Trade Responsibly")
    lines.append(f"`{coin.mint}`")

    # ── Inline keyboard ──────────────────────────────────────────────────
    keyboard: list[list[InlineKeyboardButton]] = []

    row1 = []
    if coin.chain == "robinhood":
        row1.append(InlineKeyboardButton("🦄 Buy", url=coin.buy_url))
    else:
        row1.append(InlineKeyboardButton("🚀 Buy", url=coin.buy_url))
    row1.append(InlineKeyboardButton("📊 Chart", url=coin.dexscreener_url))
    keyboard.append(row1)

    social_row = []
    web = coin.website or coin.social_links.get("website")
    if web:
        social_row.append(InlineKeyboardButton("🌐 Web", url=web))
    tw = coin.twitter or coin.social_links.get("twitter")
    if tw:
        social_row.append(InlineKeyboardButton("𝕏 Twitter", url=tw))
    tg = coin.telegram or coin.social_links.get("telegram")
    if tg:
        social_row.append(InlineKeyboardButton("💬 TG", url=tg))
    if social_row:
        keyboard.append(social_row)

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def should_send_alert(alert: Alert, mode: str = "all", min_confidence: float = 0.2) -> bool:
    """Filter alert by subscription mode and confidence."""
    if alert.score.confidence < min_confidence:
        return False
    if mode == "high":
        return alert.score.verdict.value == "BUY" or alert.score.confidence >= 0.7
    # Suppress PASS verdicts unless actual risk signals are present.
    if alert.score.verdict.value == "PASS" and not alert.signals:
        return False
    return True
