"""Application configuration loaded from environment variables."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the memecoin alert bot."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    # Comma-separated extra chat/group IDs to also receive alerts.
    telegram_chat_ids: str = Field(default="", alias="TELEGRAM_CHAT_IDS")
    webhook_url: str = Field(default="", alias="WEBHOOK_URL")
    port: int = Field(default=8000, alias="PORT")

    def get_chat_ids(self) -> list[str]:
        """Return all destination chat IDs (primary + extras), deduped."""
        ids: list[str] = []
        for raw in [self.telegram_chat_id, *self.telegram_chat_ids.split(",")]:
            cid = raw.strip()
            if cid and cid not in ids:
                ids.append(cid)
        return ids

    # API keys
    solscan_api_key: str = Field(default="", alias="SOLSCAN_API_KEY")
    rugcheck_api_key: str = Field(default="", alias="RUGCHECK_API_KEY")
    x_bearer_token: str = Field(default="", alias="X_BEARER_TOKEN")
    codex_api_key: str = Field(default="", alias="CODEX_API_KEY")
    bitquery_api_key: str = Field(default="", alias="BITQUERY_API_KEY")
    bubblemaps_api_key: str = Field(default="", alias="BUBBLEMAPS_API_KEY")

    # Bot behavior
    alert_cooldown_seconds: int = Field(default=300, alias="ALERT_COOLDOWN_SECONDS")
    min_confidence: float = Field(default=0.35, alias="MIN_CONFIDENCE")
    min_market_cap: float = Field(default=10000, alias="MIN_MARKET_CAP")
    # 0 disables the ceiling; set e.g. 100000 to focus on the 10k–100k band.
    max_market_cap: float = Field(default=0, alias="MAX_MARKET_CAP")
    min_volume_24h: float = Field(default=0, alias="MIN_VOLUME_24H")
    sol_usd: float = Field(default=170.0, alias="SOL_USD")
    # Send a follow-up "is up NX" update when an alerted token gains this %.
    moon_update_pct: float = Field(default=50.0, alias="MOON_UPDATE_PCT")
    # Maestro referral code; alerts deep-link t.me/maestro?start=<ref>-<CA>
    maestro_referral: str = Field(default="r-nittyberry0", alias="MAESTRO_REFERRAL")
    subscription_mode: str = Field(default="all", alias="SUBSCRIPTION_MODE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Data source toggles
    enable_pumpportal: bool = Field(default=True, alias="ENABLE_PUMPPORTAL")
    enable_pumpfun_rest: bool = Field(default=True, alias="ENABLE_PUMPFUN_REST")
    enable_dexscreener: bool = Field(default=True, alias="ENABLE_DEXSCREENER")
    enable_rugcheck: bool = Field(default=True, alias="ENABLE_RUGCHECK")
    enable_solscan: bool = Field(default=True, alias="ENABLE_SOLSCAN")

    # Robinhood Chain (Arbitrum Orbit L2)
    enable_pons_robinhood: bool = Field(default=True, alias="ENABLE_PONS_ROBINHOOD")
    enable_noxa_robinhood: bool = Field(default=True, alias="ENABLE_NOXA_ROBINHOOD")
    enable_direct_discovery: bool = Field(default=True, alias="ENABLE_DIRECT_DISCOVERY")
    robinhood_rpc_url: str = Field(
        default="https://rpc.mainnet.chain.robinhood.com",
        alias="ROBINHOOD_RPC_URL",
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return cached settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
