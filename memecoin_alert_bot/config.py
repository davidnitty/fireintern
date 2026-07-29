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
    webhook_url: str = Field(default="", alias="WEBHOOK_URL")
    port: int = Field(default=8000, alias="PORT")

    # API keys
    solscan_api_key: str = Field(default="", alias="SOLSCAN_API_KEY")
    rugcheck_api_key: str = Field(default="", alias="RUGCHECK_API_KEY")
    x_bearer_token: str = Field(default="", alias="X_BEARER_TOKEN")
    codex_api_key: str = Field(default="", alias="CODEX_API_KEY")
    bitquery_api_key: str = Field(default="", alias="BITQUERY_API_KEY")

    # Bot behavior
    alert_cooldown_seconds: int = Field(default=300, alias="ALERT_COOLDOWN_SECONDS")
    min_confidence: float = Field(default=0.2, alias="MIN_CONFIDENCE")
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
