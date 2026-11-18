"""Typed application settings, loaded from environment variables and an optional .env file."""

from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExecutionMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    """Runtime configuration.

    Secrets are typed as ``SecretStr`` so they never appear in ``repr`` or logs.
    Live execution is rejected at this stage of the project: it does not exist yet,
    and enabling it must never be possible through a configuration typo.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    app_env: Literal["dev", "test", "prod"] = "dev"
    execution_mode: ExecutionMode = ExecutionMode.PAPER

    database_url: SecretStr = Field(
        default=SecretStr("postgresql+asyncpg://desk:desk@localhost:5432/desk"),
        description="SQLAlchemy async URL (postgresql+asyncpg://...).",
    )
    db_pool_size: int = Field(default=5, ge=1, le=50)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    healthchecks_ping_url: SecretStr | None = None
    heartbeat_interval_seconds: float = Field(default=60.0, gt=0)

    # --- Ingestion (step M2) -------------------------------------------------------------
    ingest_enabled: bool = False
    pumpportal_ws_url: str = "wss://pumpportal.fun/api/data"
    pumpportal_api_key: SecretStr | None = None
    tracked_pools: tuple[str, ...] = ("pump",)

    # 90 min: covers the 60-min outcome label of a token first analysed at 30 min of age.
    track_window_minutes: float = Field(default=90.0, gt=0)
    track_migrated_window_minutes: float = Field(default=360.0, gt=0)
    track_min_trades: int = Field(default=5, ge=0)
    track_min_trades_window_minutes: float = Field(default=10.0, gt=0)
    max_tracked_tokens: int = Field(default=2000, ge=1)

    feed_stale_after_seconds: float = Field(default=60.0, gt=0)
    snapshot_min_interval_seconds: float = Field(default=5.0, ge=0)
    ingest_stats_interval_seconds: float = Field(default=3600.0, gt=0)

    helius_api_key: SecretStr | None = None
    helius_rpc_url: str = "https://mainnet.helius-rpc.com"
    verify_sample_rate: float = Field(default=0.02, ge=0, le=1)
    verify_max_rps: float = Field(default=2.0, gt=0)
    helius_daily_credit_budget: int = Field(default=25_000, ge=0)

    # --- Analysis (step M3) --------------------------------------------------------------
    analysis_enabled: bool = True  # effective only with ingestion and a Helius key
    analysis_interval_seconds: float = Field(default=20.0, gt=0)
    analysis_min_age_minutes: float = Field(default=3.0, ge=0)
    analysis_max_age_minutes: float = Field(default=30.0, gt=0)
    analysis_reanalyze_minutes: float = Field(default=10.0, gt=0)
    analysis_min_trades: int = Field(default=15, ge=0)
    analysis_batch: int = Field(default=10, ge=1)
    rugcheck_enabled: bool = False

    # --- Paper trading (step M4) ---------------------------------------------------------
    paper_trading_enabled: bool = True  # effective only with ingestion enabled
    paper_tick_seconds: float = Field(default=2.0, gt=0)
    jupiter_api_key: SecretStr | None = None  # quotes only; needed for strategy B
    jupiter_base_url: str = "https://api.jup.ag/swap/v2"

    @field_validator("execution_mode")
    @classmethod
    def _reject_live_mode(cls, value: ExecutionMode) -> ExecutionMode:
        if value is not ExecutionMode.PAPER:
            raise ValueError(
                "live execution is not implemented; only 'paper' is accepted at this stage"
            )
        return value

    @field_validator("database_url")
    @classmethod
    def _require_async_postgres(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use the 'postgresql+asyncpg://' scheme")
        return value

    @model_validator(mode="after")
    def _telegram_all_or_nothing(self) -> Self:
        if (self.telegram_bot_token is None) != (self.telegram_chat_id is None):
            raise ValueError("telegram_bot_token and telegram_chat_id must be set together")
        return self

    def secret_values(self) -> list[str]:
        """Plain values of every configured secret, used by the log redactor."""
        secrets = [
            self.database_url,
            self.telegram_bot_token,
            self.healthchecks_ping_url,
            self.pumpportal_api_key,
            self.helius_api_key,
            self.jupiter_api_key,
        ]
        return [s.get_secret_value() for s in secrets if s is not None and s.get_secret_value()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
