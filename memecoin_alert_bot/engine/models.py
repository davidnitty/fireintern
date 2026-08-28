"""Core data models for the signal engine."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SignalType(str, Enum):
    """Seven signal types defined in the project plan."""

    AI_AGENT = "ai_agent"
    CELEBRITY = "celebrity"
    VIRAL_MEME = "viral_meme"
    TEAM_BACKED = "team_backed"
    CTO = "cto"
    BUNDLING_RISK = "bundling_risk"
    VAMP_RISK = "vamp_risk"

    @property
    def emoji(self) -> str:
        mapping = {
            SignalType.AI_AGENT: "🤖",
            SignalType.CELEBRITY: "⭐",
            SignalType.VIRAL_MEME: "🐵",
            SignalType.TEAM_BACKED: "🚀",
            SignalType.CTO: "🤝",
            SignalType.BUNDLING_RISK: "🚨",
            SignalType.VAMP_RISK: "🧛",
        }
        return mapping[self]

    @property
    def label(self) -> str:
        mapping = {
            SignalType.AI_AGENT: "AI Agent Token",
            SignalType.CELEBRITY: "Celebrity Coin",
            SignalType.VIRAL_MEME: "Viral Meme",
            SignalType.TEAM_BACKED: "Team-Backed",
            SignalType.CTO: "Community Takeover",
            SignalType.BUNDLING_RISK: "Bundling Risk",
            SignalType.VAMP_RISK: "Vamp Risk",
        }
        return mapping[self]


class TopHolder(BaseModel):
    """A single top holder entry."""

    address: str
    pct: float = Field(default=0.0, ge=0.0, le=100.0)
    is_fresh: bool = False
    is_contract: bool = False


class SafetyInfo(BaseModel):
    """Safety metadata used by bundling and honeypot detectors."""

    model_config = ConfigDict(validate_assignment=True)

    lp_locked: bool | None = None
    lp_locked_pct: float | None = None
    mint_authority_enabled: bool | None = None
    freeze_authority_enabled: bool | None = None
    top_holders: list[TopHolder] = Field(default_factory=list)
    bundled_pct: float = 0.0
    rugcheck_score: int | None = None
    is_honeypot: bool | None = None


class CoinData(BaseModel):
    """Normalized coin data merged from multiple sources."""

    model_config = ConfigDict(validate_assignment=True)

    # Identity
    mint: str
    chain: str = "solana"
    chain_id: int | None = None
    symbol: str = "UNKNOWN"
    name: str = ""
    description: str = ""

    # Market
    market_cap: float | None = None
    volume_24h: float | None = None
    buy_volume_1h: float | None = None
    sell_volume_1h: float | None = None
    buy_pressure: float | None = None
    # Verified short-window flow fields. USD only when flow_data_quality is
    # 'verified_usd'; chain-native values are marked 'directional_only'.
    volume_5m: float | None = None
    volume_1h: float | None = None
    buys_5m: int | None = None
    sells_5m: int | None = None
    buys_1h: int | None = None
    sells_1h: int | None = None
    price_change_5m: float | None = None
    price_change_1h: float | None = None
    vl_ratio_1h: float | None = None
    flow_ratio: float | None = None
    swap_speed: float | None = None
    flow_label: str = "-"
    flow_data_quality: str = "unknown"  # verified_usd / directional_only / unknown
    liquidity: float | None = None
    price: float | None = None
    price_sol: float | None = None
    bonding_curve: float | None = None
    is_bonding_complete: bool = False

    # Holders / time
    holders: int | None = None
    age_seconds: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Dev / team
    dev_wallet: str = ""
    deployer: str | None = None
    dev_sol_balance: float | None = None
    metadata_uri: str | None = None  # Metaplex/IPFS metadata JSON from create event
    social_links: dict[str, str] = Field(default_factory=dict)
    website: str | None = None
    twitter: str | None = None
    telegram: str | None = None

    # EVM / pool specifics
    pool_address: str | None = None
    pair_token: str | None = None

    # Agent / narrative
    tokenized_agent: bool = False
    ai_keywords: list[str] = Field(default_factory=list)
    narrative_keywords: list[str] = Field(default_factory=list)
    narrative_strength: float = 0.0
    vamp_similarity: float = 0.0  # 0-1, how similar to another token's narrative

    # Safety
    safety: SafetyInfo = Field(default_factory=SafetyInfo)

    # Raw provenance
    sources: dict[str, Any] = Field(default_factory=dict)

    @property
    def vol_mc_ratio(self) -> float:
        """Volume to market-cap ratio (SPYZER bundling heuristic)."""
        if self.market_cap and self.market_cap > 0 and self.volume_24h is not None:
            return self.volume_24h / self.market_cap
        return 0.0

    @property
    def top_holder_pct(self) -> float:
        """Return the largest single holder percentage."""
        if self.safety.top_holders:
            return max((h.pct for h in self.safety.top_holders), default=0.0)
        return 0.0

    @property
    def top10_holder_pct(self) -> float:
        """Return combined percentage of the top 10 holders."""
        return sum(h.pct for h in self.safety.top_holders[:10])

    @property
    def pump_fun_url(self) -> str:
        return f"https://pump.fun/coin/{self.mint}"

    @property
    def dexscreener_url(self) -> str:
        chain_slug = "solana" if self.chain == "solana" else self.chain
        return f"https://dexscreener.com/{chain_slug}/{self.mint}"

    @property
    def buy_url(self) -> str:
        """Return the best buy link for the coin's chain."""
        if self.chain == "solana":
            return self.pump_fun_url
        if self.pool_address:
            return f"https://dexscreener.com/{self.chain}/{self.pool_address}"
        return self.dexscreener_url


class Signal(BaseModel):
    """A detected signal attached to a coin."""

    signal_type: SignalType
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)

    @property
    def display(self) -> str:
        return f"{self.signal_type.emoji} {self.signal_type.label}"


class Verdict(str, Enum):
    BUY = "BUY"
    WAIT = "WAIT"
    DYOR = "DYOR"
    PASS = "PASS"


class Tier(str, Enum):
    """Relative quality/risk tier (guide §4). Relative only — not a safety claim."""

    DIAMOND = "DIAMOND"            # High Relative Quality
    STANDARD = "STANDARD"          # Watch
    GAMBLE = "GAMBLE"              # Speculative
    HIGH_RISK = "HIGH_RISK"        # Reject / suppress

    @property
    def emoji(self) -> str:
        return {
            Tier.DIAMOND: "💎",
            Tier.STANDARD: "🔵",
            Tier.GAMBLE: "🎲",
            Tier.HIGH_RISK: "",
        }[self]

    @property
    def label(self) -> str:
        return {
            Tier.DIAMOND: "HIGH RELATIVE QUALITY",
            Tier.STANDARD: "WATCH",
            Tier.GAMBLE: "SPECULATIVE",
            Tier.HIGH_RISK: "HIGH RISK / REJECT",
        }[self]


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    EXTREME = "EXTREME"

    @property
    def emoji(self) -> str:
        return {
            RiskLevel.LOW: "🟢",
            RiskLevel.MEDIUM: "🟡",
            RiskLevel.HIGH: "🟠",
            RiskLevel.EXTREME: "🔴",
        }[self]


class ScoreBreakdown(BaseModel):
    """Per-variable contribution to the composite score."""

    bundling: float = 0.0
    dev_wallet: float = 0.0
    narrative: float = 0.0
    liquidity: float = 0.0
    market_conditions: float = 0.0
    holders: float = 0.0
    chart_structure: float = 0.0
    buying_pressure: float = 0.0


class GateResult(BaseModel):
    """Outcome of a single hard gate (guide §3.2)."""

    gate: str
    passed: bool
    status: str = "unknown"  # passed / failed / unknown
    detail: str = ""


class ScoreResult(BaseModel):
    """Final scoring result for a coin.

    Follows the revised guide: separate Quality (Q), Risk (R), and Data
    Confidence (C) rather than a single unexplained composite.
    """

    composite_score: float = 0.0
    confidence: float = 0.0
    verdict: Verdict = Verdict.PASS
    risk: RiskLevel = RiskLevel.LOW

    # Revised three-axis scores (0-100)
    quality: float = 0.0
    risk_score: float = 0.0
    data_confidence: float = 0.0

    tier: Tier = Tier.HIGH_RISK
    gates: list[GateResult] = Field(default_factory=list)
    gates_passed: bool = False
    gates_unknown: bool = False  # unknown critical gate caps tier at STANDARD
    invalidation: list[str] = Field(default_factory=list)

    breakdown: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    explanation: list[str] = Field(default_factory=list)


class Alert(BaseModel):
    """An alert ready to be sent to Telegram."""

    coin: CoinData
    signals: list[Signal] = Field(default_factory=list)
    score: ScoreResult = Field(default_factory=ScoreResult)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def primary_signal(self) -> str:
        if self.signals:
            return self.signals[0].display
        return "ℹ️ General"
