from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Extended API — all four required by SDK initializer
    # NOTE: private_key and public_key are passed to the SDK but never used for
    # signing — this is a read-only system; only api_key is sent in HTTP requests.
    extended_api_key: str
    extended_public_key: str
    extended_private_key: str
    extended_vault: str
    extended_client_id: str | None = None  # X-Client-Id header; defaults to extended_vault if unset
    extended_network: str = "mainnet"

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str

    # Polling intervals per endpoint group
    poll_interval_orders_seconds: int = Field(default=60, ge=1)
    poll_interval_positions_seconds: int = Field(default=60, ge=1)
    poll_interval_history_seconds: int = Field(default=60, ge=1)

    # Persistence
    state_db_path: str = "/data/state.db"
    notification_dedup_ttl_days: int = Field(default=30, ge=1)

    # Notification toggles
    enable_order_opened: bool = True
    enable_order_updated: bool = True
    enable_order_filled: bool = True
    enable_position_opened: bool = True
    enable_position_updated: bool = True
    enable_position_closed: bool = True
    enable_startup_notification: bool = True

    # Optional thresholds
    unrealized_pnl_threshold_usdc: float | None = None

    @field_validator("unrealized_pnl_threshold_usdc", "extended_client_id", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"
