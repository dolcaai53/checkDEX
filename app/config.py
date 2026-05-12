from __future__ import annotations

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Exchange selector — comma-separated list, e.g. "extended" or "extended,hyperliquid"
    active_exchanges: list[str] = ["extended"]

    # Extended API — required only when active_exchange == "extended"
    # NOTE: private_key and public_key are passed to the SDK but never used for
    # signing — this is a read-only system; only api_key is sent in HTTP requests.
    extended_api_key: str = ""
    extended_public_key: str = ""
    extended_private_key: str = ""
    extended_vault: str = ""
    extended_client_id: str | None = None  # X-Client-Id header; defaults to extended_vault if unset
    extended_network: str = "mainnet"

    # Hyperliquid — required only when active_exchange == "hyperliquid"
    hyperliquid_wallet_address: str | None = None  # 0x... public wallet address; no private key needed
    hyperliquid_testnet: bool = False

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

    # Daily position summary
    enable_daily_summary: bool = True
    daily_summary_time: str = "08:00"  # HH:MM UTC

    @field_validator("active_exchanges", mode="before")
    @classmethod
    def _parse_exchanges(cls, v: object) -> object:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator(
        "unrealized_pnl_threshold_usdc",
        "extended_client_id",
        "hyperliquid_wallet_address",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("daily_summary_time")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        try:
            parts = v.split(":")
            if len(parts) != 2:
                raise ValueError
            hour, minute = int(parts[0]), int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except (ValueError, AttributeError):
            raise ValueError(f"DAILY_SUMMARY_TIME must be HH:MM (UTC), got: {v!r}")
        return v

    @model_validator(mode="after")
    def _validate_exchange_config(self) -> "Config":
        if not self.active_exchanges:
            raise ValueError("ACTIVE_EXCHANGES must not be empty")
        valid = {"extended", "hyperliquid"}
        for exchange in self.active_exchanges:
            if exchange not in valid:
                raise ValueError(
                    f"Unknown exchange {exchange!r} in ACTIVE_EXCHANGES. "
                    f"Valid values: {', '.join(sorted(valid))}"
                )
            if exchange == "extended":
                missing = [
                    name
                    for name, val in [
                        ("EXTENDED_API_KEY", self.extended_api_key),
                        ("EXTENDED_PUBLIC_KEY", self.extended_public_key),
                        ("EXTENDED_PRIVATE_KEY", self.extended_private_key),
                        ("EXTENDED_VAULT", self.extended_vault),
                    ]
                    if not val
                ]
                if missing:
                    raise ValueError(f"Missing required Extended config: {', '.join(missing)}")
            elif exchange == "hyperliquid":
                if not self.hyperliquid_wallet_address:
                    raise ValueError(
                        "HYPERLIQUID_WALLET_ADDRESS is required when hyperliquid is in ACTIVE_EXCHANGES"
                    )
        return self

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"
