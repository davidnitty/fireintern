"""Seven signal detectors based on the SPYZER framework."""

from __future__ import annotations

import re
from typing import Callable

from memecoin_alert_bot.engine.models import CoinData, Signal, SignalType

# Keyword lexicons
AI_KEYWORDS = [
    "ai",
    "agent",
    "autonomous",
    "bot",
    "gpt",
    "llm",
    "neural",
    "machine learning",
    "agent token",
    "ai trading",
    "no human",
    "fable",
    "aixbt",
]

CELEBRITY_KEYWORDS = [
    "trump",
    "elon",
    "musk",
    "kanye",
    "snoop",
    "caitlyn",
    "andrew tate",
    "celebrity",
    "president",
    "official",
]

ANIMAL_KEYWORDS = [
    "dog",
    "cat",
    "frog",
    "ape",
    "monkey",
    "bird",
    "mouse",
    "hamster",
    "penguin",
    "moose",
    "hippo",
    "panda",
    "tiger",
    "lion",
    "bear",
    "rabbit",
    "punch",
    "moodeng",
    "pepe",
    "bonk",
    "shib",
    "doge",
    "wif",
]

TEAM_KEYWORDS = [
    "team",
    "dev team",
    "marketing",
    "partnership",
    "audit",
    "roadmap",
    "whitepaper",
    "kyc",
    "cex listing",
    "launchpad",
]

CTO_KEYWORDS = [
    "community takeover",
    "dev rugged",
    "dev sold",
    "dev left",
    "cto",
    "we took over",
    "community revived",
]


def _contains_keyword(text: str, keywords: list[str]) -> list[str]:
    """Return matched keywords found in text."""
    text_lower = text.lower()
    found = []
    for kw in keywords:
        # Use word boundaries for short keywords
        if len(kw) <= 4:
            if re.search(rf"\b{re.escape(kw)}\b", text_lower):
                if kw not in found:
                    found.append(kw)
        else:
            if kw in text_lower and kw not in found:
                found.append(kw)
    return found


def _safe_pct(value: float | None) -> float:
    return value if value is not None else 0.0


class Detector:
    """Base detector returning a Signal or None."""

    def __init__(self, signal_type: SignalType, fn: Callable[[CoinData], Signal | None]):
        self.signal_type = signal_type
        self.fn = fn

    def detect(self, coin: CoinData) -> Signal | None:
        return self.fn(coin)


# --- Individual detectors -----------------------------------------------------


def detect_ai_agent(coin: CoinData) -> Signal | None:
    """Detect AI agent tokens via tokenizedAgent flag and keyword matches."""
    reasons = []
    confidence = 0.0

    if coin.tokenized_agent or coin.ai_keywords:
        confidence += 0.5
        label = "Tokenized Agent flag" if coin.tokenized_agent else "AI keyword match"
        keys = ", ".join(coin.ai_keywords[:3]) if coin.ai_keywords else ""
        reasons.append(f"{label}{f': {keys}' if keys else ''}")

    text = f"{coin.name} {coin.description}"
    matched = _contains_keyword(text, AI_KEYWORDS)
    if matched:
        confidence += min(0.4, len(matched) * 0.15)
        reasons.append(f"AI narrative keywords: {', '.join(matched[:3])}")

    if coin.social_links.get("twitter") and any(
        k in coin.social_links["twitter"].lower() for k in ["ai", "agent", "bot"]
    ):
        confidence += 0.1
        reasons.append("AI-aligned social handle")

    if confidence <= 0.25:
        return None

    return Signal(
        signal_type=SignalType.AI_AGENT,
        confidence=min(confidence, 1.0),
        reasons=reasons,
    )


def detect_celebrity(coin: CoinData) -> Signal | None:
    """Detect celebrity or politician-endorsed coins."""
    text = f"{coin.name} {coin.description} {coin.symbol}"
    matched = _contains_keyword(text, CELEBRITY_KEYWORDS)
    if not matched:
        return None

    confidence = 0.4 + min(0.5, len(matched) * 0.15)
    rapid_growth = (
        coin.market_cap is not None
        and coin.volume_24h is not None
        and coin.market_cap > 100_000
        and coin.vol_mc_ratio > 1.0
    )
    reasons = [f"Celebrity keyword match: {', '.join(matched[:3])}"]
    if rapid_growth:
        confidence += 0.2
        reasons.append("Rapid market-cap/volume growth")

    return Signal(signal_type=SignalType.CELEBRITY, confidence=min(confidence, 1.0), reasons=reasons)


def detect_viral_meme(coin: CoinData) -> Signal | None:
    """Detect animal/character memes with viral momentum."""
    text = f"{coin.name} {coin.description}"
    matched = _contains_keyword(text, ANIMAL_KEYWORDS)
    if not matched:
        return None

    confidence = 0.35
    reasons = [f"Meme character keyword: {', '.join(matched[:3])}"]

    if coin.market_cap and 10_000 <= coin.market_cap <= 5_000_000:
        confidence += 0.15
        reasons.append("Early-stage meme market cap")

    if coin.vol_mc_ratio and coin.vol_mc_ratio > 0.8:
        confidence += 0.15
        reasons.append("Strong volume/market-cap ratio")

    if coin.holders and coin.holders >= 500:
        confidence += 0.15
        reasons.append(f"{coin.holders:,} holders already")

    if coin.age_seconds is not None and coin.age_seconds <= 3600:
        confidence += 0.2
        reasons.append("Launched within last hour (first-mover window)")

    return Signal(
        signal_type=SignalType.VIRAL_MEME,
        confidence=min(confidence, 1.0),
        reasons=reasons,
    )


def detect_team_backed(coin: CoinData) -> Signal | None:
    """Detect organized team launches via social links and narrative."""
    score = 0.0
    reasons = []

    social_count = len(coin.social_links)
    if social_count >= 2:
        score += 0.25
        reasons.append(f"Multiple social links ({social_count})")

    web = coin.website or coin.social_links.get("website")
    if web:
        score += 0.15
        reasons.append("Website present")

    text = f"{coin.name} {coin.description}"
    matched = _contains_keyword(text, TEAM_KEYWORDS)
    if matched:
        score += min(0.3, len(matched) * 0.1)
        reasons.append(f"Team/roadmap language: {', '.join(matched[:3])}")

    # Holder distribution: active accumulators look different from bots.
    if coin.holders and coin.holders > 1_000:
        score += 0.15
        reasons.append("Broad holder base")

    if score <= 0.25:
        return None
    return Signal(signal_type=SignalType.TEAM_BACKED, confidence=min(score, 0.95), reasons=reasons)


def detect_cto(coin: CoinData) -> Signal | None:
    """Detect Community Takeover narratives."""
    text = f"{coin.name} {coin.description}"
    matched = _contains_keyword(text, CTO_KEYWORDS)
    if not matched:
        return None

    confidence = 0.45
    reasons = [f"CTO keyword: {', '.join(matched[:3])}"]

    # If the original dev wallet has no balance and holders keep growing,
    # it supports the community-took-over story.
    if coin.dev_wallet and (coin.dev_sol_balance is not None and coin.dev_sol_balance < 0.1):
        confidence += 0.2
        reasons.append("Original dev wallet appears inactive")

    if coin.holders and coin.holders > 500:
        confidence += 0.15
        reasons.append("Community still growing after dev exit")

    return Signal(signal_type=SignalType.CTO, confidence=min(confidence, 1.0), reasons=reasons)


def detect_bundling_risk(coin: CoinData) -> Signal | None:
    """Detect supply bundling and related scam risk."""
    reasons = []
    risk_factors = 0

    top = coin.top_holder_pct
    if top > 3.5:
        risk_factors += 1
        reasons.append(f"Top holder owns {top:.2f}% (>3.5%)")

    top10 = coin.top10_holder_pct
    if top10 > 50:
        risk_factors += 1
        reasons.append(f"Top 10 holders own {top10:.2f}% (>50%)")

    if coin.vol_mc_ratio and coin.vol_mc_ratio < 0.8 and coin.market_cap and coin.market_cap > 0:
        risk_factors += 1
        reasons.append(f"VOL/MC {coin.vol_mc_ratio:.2f} (<0.8)")

    if coin.safety.bundled_pct > 10:
        risk_factors += 1
        reasons.append(f"Rugcheck flagged {coin.safety.bundled_pct:.2f}% bundled")

    fresh_holders = sum(1 for h in coin.safety.top_holders if h.is_fresh)
    if fresh_holders >= 3:
        risk_factors += 1
        reasons.append(f"{fresh_holders} fresh-wallet top holders")

    if risk_factors == 0:
        return None

    confidence = min(0.95, 0.35 + risk_factors * 0.15)
    return Signal(
        signal_type=SignalType.BUNDLING_RISK,
        confidence=confidence,
        reasons=reasons,
    )


def detect_vamp_risk(coin: CoinData) -> Signal | None:
    """Detect narrative competition / vamp risk using TF-IDF similarity."""
    reasons = []
    confidence = 0.0

    # Primary signal: cosine similarity to another recent token.
    if coin.vamp_similarity > 0.7:
        confidence += 0.5
        reasons.append(f"High narrative similarity ({coin.vamp_similarity:.2f})")
    elif coin.vamp_similarity > 0.5:
        confidence += 0.25
        reasons.append(f"Moderate narrative similarity ({coin.vamp_similarity:.2f})")

    # Legacy: generic/copycat qualifier words in name.
    name_lower = coin.name.lower()
    generic_terms = ["official", "real", "true", "original", "new", "v2", "2.0", "pro"]
    generic_hits = [t for t in generic_terms if t in name_lower]
    if generic_hits:
        confidence += 0.2
        reasons.append(f"Copycat qualifiers: {', '.join(generic_hits)}")

    # Narrative keywords exist but narrative is weak.
    if coin.narrative_keywords and coin.narrative_strength < 0.3:
        confidence += 0.15
        reasons.append("Weak narrative differentiation")

    if confidence <= 0.25:
        return None
    return Signal(signal_type=SignalType.VAMP_RISK, confidence=min(confidence, 0.9), reasons=reasons)


# --- Detector registry --------------------------------------------------------


def build_detectors() -> list[Detector]:
    """Return the ordered list of signal detectors."""
    return [
        Detector(SignalType.AI_AGENT, detect_ai_agent),
        Detector(SignalType.CELEBRITY, detect_celebrity),
        Detector(SignalType.VIRAL_MEME, detect_viral_meme),
        Detector(SignalType.TEAM_BACKED, detect_team_backed),
        Detector(SignalType.CTO, detect_cto),
        Detector(SignalType.BUNDLING_RISK, detect_bundling_risk),
        Detector(SignalType.VAMP_RISK, detect_vamp_risk),
    ]


def run_all(coin: CoinData) -> list[Signal]:
    """Run every detector and return triggered signals sorted by confidence."""
    signals = []
    for detector in build_detectors():
        signal = detector.detect(coin)
        if signal:
            signals.append(signal)
    return sorted(signals, key=lambda s: s.confidence, reverse=True)
