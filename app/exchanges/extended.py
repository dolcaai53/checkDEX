from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp

import dataclasses

from x10.errors import X10Error
from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.configuration import MAINNET_CONFIG, TESTNET_CONFIG
from x10.perpetual.orders import OpenOrderModel
from x10.perpetual.orders import OrderSide as SdkOrderSide
from x10.perpetual.positions import PositionHistoryModel, PositionModel
from x10.perpetual.trading_client.trading_client import PerpetualTradingClient
from x10.perpetual.trades import AccountTradeModel
from x10.utils.http import CLIENT_TIMEOUT, ResponseStatus, WrappedApiResponse

from app.config import Config
from app.exceptions import ExchangeAPIError, ExchangeConnectionError
from app.exchanges.base import ExchangeAdapter
from app.models.order import Order, OrderSide, OrderStatus, OrderType
from app.models.position import Position, PositionSide
from app.models.trade import Trade
from app.utils.retry import with_retry

logger = logging.getLogger(__name__)

# Number of records fetched per history poll. Covers all events in a 60 s window
# under normal trading conditions. Increase if many events per minute are expected.
_HISTORY_FETCH_LIMIT = 50

# String-keyed maps — pydantic v2 with StrEnum stores the string value in model
# fields, so sdk_model.status returns "NEW" not SdkOrderStatus.NEW.
_ORDER_STATUS_MAP: dict[str, OrderStatus] = {
    "UNKNOWN": OrderStatus.OPEN,
    "NEW": OrderStatus.OPEN,
    "UNTRIGGERED": OrderStatus.OPEN,
    "PARTIALLY_FILLED": OrderStatus.PARTIAL_FILL,
    "FILLED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "EXPIRED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}

_ORDER_TYPE_MAP: dict[str, OrderType] = {
    "LIMIT": OrderType.LIMIT,
    "MARKET": OrderType.MARKET,
    "CONDITIONAL": OrderType.CONDITIONAL,
    "TPSL": OrderType.TPSL,
}


def _unix_ms_to_utc(ts: int) -> datetime:
    """Convert Unix timestamp (milliseconds or seconds) to UTC datetime."""
    if ts > 1_000_000_000_000:
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _unwrap(response: WrappedApiResponse, label: str):
    """Extract data from WrappedApiResponse or raise ExchangeAPIError."""
    # Pydantic v2 may deserialise the status field as a plain string instead of the
    # ResponseStatus enum, so check both the enum value and its string equivalent.
    status_ok = response.status in (ResponseStatus.OK, ResponseStatus.OK.value)
    if not status_ok or response.data is None:
        error_msg = str(response.error) if response.error else "unknown error"
        raise ExchangeAPIError(f"{label} failed: {error_msg}")
    return response.data


def _map_raw_order(raw: dict, exchange: str) -> Order:
    """Map raw JSON order dict to internal Order, tolerating missing price (MARKET orders).

    The raw API uses camelCase field names (filledQty, createdTime, updatedTime).
    """
    price_raw = raw.get("price")
    price = Decimal(str(price_raw)) if price_raw not in (None, "", "0", "0.0", "0.0000000000000000") else None
    return Order(
        id=str(raw["id"]),
        exchange=exchange,
        market=str(raw["market"]),
        side=OrderSide(str(raw["side"]).upper()),
        type=_ORDER_TYPE_MAP.get(str(raw.get("type", "LIMIT")).upper(), OrderType.LIMIT),
        price=price,
        qty=Decimal(str(raw["qty"])),
        filled_qty=Decimal(str(raw.get("filledQty") or "0")),
        status=_ORDER_STATUS_MAP.get(str(raw.get("status", "NEW")).upper(), OrderStatus.OPEN),
        created_at=_unix_ms_to_utc(raw["createdTime"]) if raw.get("createdTime") else None,
        updated_at=_unix_ms_to_utc(raw["updatedTime"]) if raw.get("updatedTime") else None,
    )


def map_order(sdk_order: OpenOrderModel, exchange: str) -> Order:
    """Map SDK OpenOrderModel to internal Order model."""
    return Order(
        id=str(sdk_order.id),
        exchange=exchange,
        market=sdk_order.market,
        side=OrderSide(str(sdk_order.side)),
        type=_ORDER_TYPE_MAP.get(str(sdk_order.type), OrderType.LIMIT),
        price=sdk_order.price,
        qty=sdk_order.qty,
        filled_qty=sdk_order.filled_qty or Decimal("0"),
        status=_ORDER_STATUS_MAP.get(str(sdk_order.status), OrderStatus.OPEN),
        created_at=_unix_ms_to_utc(sdk_order.created_time),
        updated_at=_unix_ms_to_utc(sdk_order.updated_time),
    )


def map_position(sdk_pos: PositionModel, exchange: str) -> Position:
    """Map SDK PositionModel to internal Position model."""
    return Position(
        market=sdk_pos.market,
        exchange=exchange,
        side=PositionSide(str(sdk_pos.side)),
        size=sdk_pos.size,
        entry_price=sdk_pos.open_price,
        mark_price=sdk_pos.mark_price,
        leverage=sdk_pos.leverage,
        unrealized_pnl=sdk_pos.unrealised_pnl,
        opened_at=_unix_ms_to_utc(sdk_pos.created_at),
    )


def map_position_history(sdk_hist: PositionHistoryModel, exchange: str) -> Trade:
    """Map SDK PositionHistoryModel to internal Trade model (closed position)."""
    return Trade(
        id=str(sdk_hist.id),
        exchange=exchange,
        market=sdk_hist.market,
        side=PositionSide(str(sdk_hist.side)),
        size=sdk_hist.size,
        entry_price=sdk_hist.open_price,
        exit_price=sdk_hist.exit_price,
        realised_pnl=sdk_hist.realised_pnl,
        opened_at=_unix_ms_to_utc(sdk_hist.created_time) if sdk_hist.created_time else None,
        closed_at=_unix_ms_to_utc(sdk_hist.closed_time) if sdk_hist.closed_time else _unix_ms_to_utc(0),
    )


def map_trade(sdk_trade: AccountTradeModel, exchange: str) -> Trade:
    """Map SDK AccountTradeModel (individual fill) to internal Trade model.

    NOTE: AccountTradeModel represents a single fill event, not a closed position.
    It lacks entry_price/exit_price in the position-close sense. We set entry_price
    and exit_price both to the fill price and realised_pnl to 0 — this is only
    used for fill event tracking, not for PnL calculation.
    """
    side = PositionSide.LONG if str(sdk_trade.side) == SdkOrderSide.BUY else PositionSide.SHORT
    return Trade(
        id=str(sdk_trade.id),
        exchange=exchange,
        market=sdk_trade.market,
        side=side,
        size=sdk_trade.qty,
        entry_price=sdk_trade.price,
        exit_price=sdk_trade.price,
        realised_pnl=Decimal("0"),
        opened_at=None,
        closed_at=_unix_ms_to_utc(sdk_trade.created_time),
    )


class ExtendedAdapter(ExchangeAdapter):
    """Exchange adapter for Extended Exchange (x10-python-trading SDK).

    Authentication: StarkPerpetualAccount requires vault, private_key, public_key,
    and api_key. For all read-only calls only the api_key is sent in the
    X-Api-Key HTTP header — private_key is never used for signing in this system.

    Pagination: Extended SDK does not support time-based filtering. History methods
    always fetch the most recent _HISTORY_FETCH_LIMIT records. The event engine
    uses ID-based deduplication to avoid reprocessing.

    The *since* parameter accepted by history methods is kept for interface
    compatibility with future adapters (e.g. Hyperliquid) that support it.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client: PerpetualTradingClient | None = None
        self._session: aiohttp.ClientSession | None = None
        self._api_base_url: str = ""

    @property
    def exchange_name(self) -> str:
        return "Extended"

    @property
    def network(self) -> str:
        return self._config.extended_network

    async def connect(self) -> None:
        # The SDK ships with outdated mainnet URLs; patch both with the current domain.
        if self._config.extended_network == "mainnet":
            sdk_config = dataclasses.replace(
                MAINNET_CONFIG,
                api_base_url="https://api.starknet.extended.exchange/api/v1",
                onboarding_url="https://api.starknet.extended.exchange",
            )
        else:
            sdk_config = TESTNET_CONFIG
        account = StarkPerpetualAccount(
            vault=self._config.extended_vault,
            private_key=self._config.extended_private_key,
            public_key=self._config.extended_public_key,
            api_key=self._config.extended_api_key,
        )
        self._api_base_url = sdk_config.api_base_url
        self._client = PerpetualTradingClient(sdk_config, stark_account=account)

        # Extended Exchange requires X-Client-Id (= vault) on all authenticated requests.
        # The SDK doesn't support this header natively, so we inject a pre-built aiohttp
        # session with the header as a default. The SDK's BaseModule stores the session in
        # _BaseModule__session (Python name-mangling for __session inside a class body).
        client_id = self._config.extended_client_id or self._config.extended_vault
        self._session = aiohttp.ClientSession(
            headers={
                "X-Client-Id": str(client_id),
                "X-Api-Key": self._config.extended_api_key,
                "Accept": "application/json",
            },
            timeout=CLIENT_TIMEOUT,
        )
        self._client.account._BaseModule__session = self._session

        logger.info(
            "Extended adapter initialised",
            extra={"network": self._config.extended_network},
        )

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            logger.info("Extended adapter disconnected")

    def _client_or_raise(self) -> PerpetualTradingClient:
        if self._client is None:
            raise ExchangeConnectionError("ExtendedAdapter not connected — call connect() first")
        return self._client

    async def get_open_orders(self) -> list[Order]:
        client = self._client_or_raise()
        try:
            response = await with_retry(
                lambda: client.account.get_open_orders(),
                label="get_open_orders",
            )
        except (aiohttp.ClientError, X10Error) as exc:
            raise ExchangeConnectionError(f"get_open_orders failed: {exc}") from exc

        data = _unwrap(response, "get_open_orders")
        orders = [map_order(o, self.exchange_name) for o in data]
        logger.debug("Fetched open orders", extra={"count": len(orders)})
        return orders

    async def get_positions(self) -> list[Position]:
        client = self._client_or_raise()
        try:
            response = await with_retry(
                lambda: client.account.get_positions(),
                label="get_positions",
            )
        except (aiohttp.ClientError, X10Error) as exc:
            raise ExchangeConnectionError(f"get_positions failed: {exc}") from exc

        data = _unwrap(response, "get_positions")
        positions = [map_position(p, self.exchange_name) for p in data]
        logger.debug("Fetched positions", extra={"count": len(positions)})
        return positions

    async def get_orders_history(self, since: datetime) -> list[Order]:
        """Fetch recent order history.

        *since* is accepted for interface compatibility but ignored — Extended SDK
        does not support time-based filtering. Returns the most recent
        _HISTORY_FETCH_LIMIT records; deduplication is handled by the event engine.

        Uses a raw HTTP call instead of the SDK model to handle MARKET orders that
        omit the price field (which the SDK's OpenOrderModel requires as non-optional).
        """
        if self._session is None:
            raise ExchangeConnectionError("ExtendedAdapter not connected — call connect() first")

        url = f"{self._api_base_url}/user/orders/history"
        params = {"limit": str(_HISTORY_FETCH_LIMIT)}

        try:
            async with self._session.get(url, params=params) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        except aiohttp.ClientError as exc:
            raise ExchangeConnectionError(f"get_orders_history failed: {exc}") from exc

        orders: list[Order] = []
        for raw in payload.get("data") or []:
            try:
                orders.append(_map_raw_order(raw, self.exchange_name))
            except Exception:
                logger.debug("Skipping unparseable order history entry", extra={"id": raw.get("id")})
        logger.debug("Fetched orders history", extra={"count": len(orders)})
        return orders

    async def get_positions_history(self, since: datetime) -> list[Trade]:
        """Fetch recent closed position history (with realised PnL).

        *since* is accepted for interface compatibility but ignored — Extended SDK
        does not support time-based filtering. Returns the most recent
        _HISTORY_FETCH_LIMIT records; deduplication is handled by the event engine.
        """
        client = self._client_or_raise()
        try:
            response = await with_retry(
                lambda: client.account.get_positions_history(limit=_HISTORY_FETCH_LIMIT),
                label="get_positions_history",
            )
        except (aiohttp.ClientError, X10Error) as exc:
            raise ExchangeConnectionError(f"get_positions_history failed: {exc}") from exc

        data = _unwrap(response, "get_positions_history")
        trades = [map_position_history(p, self.exchange_name) for p in data if p.closed_time]
        logger.debug("Fetched positions history", extra={"count": len(trades)})
        return trades

    async def get_trades(self, since: datetime) -> list[Trade]:
        """Fetch recent individual fill events.

        *since* is accepted for interface compatibility but ignored — Extended SDK
        does not support time-based filtering.
        """
        client = self._client_or_raise()
        try:
            response = await with_retry(
                lambda: client.account.get_trades(limit=_HISTORY_FETCH_LIMIT),
                label="get_trades",
            )
        except (aiohttp.ClientError, X10Error) as exc:
            raise ExchangeConnectionError(f"get_trades failed: {exc}") from exc

        data = _unwrap(response, "get_trades")
        trades = [map_trade(t, self.exchange_name) for t in data]
        logger.debug("Fetched trades", extra={"count": len(trades)})
        return trades
