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


def _audit_score(score) -> int:
    """Map risk level to a 0-10 audit score (higher = safer)."""
    return {RiskLevel.LOW: 8, RiskLevel.MEDIUM: 6, RiskLevel.HIGH: 4, RiskLevel.EXTREME: 2}.get(score.risk, 5)


def _short_addr(addr: str) -> str:
    """0x1234...5678 form."""
    if not addr:
        return "N/A"
    return shorten_address(addr, 4)


# ── Main formatter ────────────────────────────────────────────────────────


def _gmgn_url(coin) -> str | None:
    """GMGN tracker link; only chains GMGN actually lists."""
    if coin.chain == "solana":
        return f"https://gmgn.ai/sol/token/{coin.mint}"
    return None  # Robinhood Chain is not on GMGN yet


def _fmt_mc_compact(value: float | None) -> str:
    """Market cap like 10K, 163K, 1.3M (matches the sample card)."""
    if value is None:
        return "N/A"
    if value >= 1_000_000:
        text = f"{value / 1_000_000:.1f}M"
    elif value >= 1_000:
        text = f"{value / 1_000:.1f}K"
    else:
        text = f"{value:.0f}"
    return text.rstrip("0").rstrip(".").replace(".0K", "K").replace(".0M", "M")


def format_alert(alert: Alert) -> tuple[str, InlineKeyboardMarkup]:
    """Build the minimal 'Intern Signal Call' card."""
    coin = alert.coin

    display_name = coin.name or coin.symbol or "Unknown"
    age = _fmt_age(coin.age_seconds).upper()

    lines: list[str] = [
        "Intern Signal Call",
        "",
        f"🚀 Token Name: {display_name}",
        f"💲 Ticker: {coin.symbol}",
        f"📊 Market Cap: {_fmt_mc_compact(coin.market_cap)}",
        f"⏱ Age: {age}",
        "",
        "🔗 Contract Address:",
        f"`{coin.mint}`",
    ]

    keyboard: list[list[InlineKeyboardButton]] = []
    row = [InlineKeyboardButton("DEX", url=coin.dexscreener_url)]
    gmgn = _gmgn_url(coin)
    if gmgn:
        row.append(InlineKeyboardButton("GMGN", url=gmgn))
    keyboard.append(row)

    newline = chr(10)
    return newline.join(lines), InlineKeyboardMarkup(keyboard)


def should_send_alert(alert: Alert, mode: str = "all", min_confidence: float = 0.2) -> bool:
    """Filter alert by tier, mode, and confidence (guide §4)."""
    from memecoin_alert_bot.engine.models import Tier

    # Hard-gate failure / extreme risk → suppress by default.
    if alert.score.tier == Tier.HIGH_RISK:
        return False
    if alert.score.confidence < min_confidence:
        return False
    if mode == "high":
        return alert.score.tier == Tier.DIAMOND or alert.score.confidence >= 0.7
    # Suppress PASS verdicts unless actual risk signals are present.
    if alert.score.verdict.value == "PASS" and not alert.signals:
        return False
    return True
