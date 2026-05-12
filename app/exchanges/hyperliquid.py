from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from app.config import Config
from app.exceptions import ExchangeConnectionError
from app.exchanges.base import ExchangeAdapter
from app.models.order import Order, OrderSide, OrderStatus, OrderType
from app.models.position import Position, PositionSide
from app.models.trade import Trade

logger = logging.getLogger(__name__)

_MAINNET_URL = "https://api.hyperliquid.xyz"
_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"

_HL_BUY = "B"
_HL_SELL = "A"

_ORDER_STATUS_MAP: dict[str, OrderStatus] = {
    "open": OrderStatus.OPEN,
    "filled": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "marginCancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
}


def _unix_ms_to_utc(ts: int | float) -> datetime:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)


def _normalize_market(coin: str) -> str:
    """Normalize Hyperliquid coin name (e.g. 'BTC') to 'BTC-USDC' market pair format."""
    return f"{coin}-USDC"


def map_order(hl_order: dict, exchange: str) -> Order:
    """Map Hyperliquid open order dict to internal Order model.

    The SDK wraps the order in {"order": {...}, "status": "open"}.
    This function handles both the wrapped and unwrapped forms.
    """
    inner = hl_order.get("order", hl_order)
    side = OrderSide.BUY if inner.get("side") == _HL_BUY else OrderSide.SELL

    orig_sz = Decimal(str(inner.get("origSz") or inner["sz"]))
    remaining_sz = Decimal(str(inner["sz"]))
    filled_qty = orig_sz - remaining_sz if orig_sz > remaining_sz else Decimal("0")
    status = OrderStatus.PARTIAL_FILL if filled_qty > 0 else OrderStatus.OPEN

    price_raw = inner.get("limitPx")
    price = (
        Decimal(str(price_raw))
        if price_raw and str(price_raw) not in ("0", "0.0", "")
        else None
    )

    ts = inner.get("timestamp") or hl_order.get("statusTimestamp")
    created_at = _unix_ms_to_utc(ts) if ts else datetime.now(timezone.utc)

    return Order(
        id=str(inner["oid"]),
        exchange=exchange,
        market=_normalize_market(inner["coin"]),
        side=side,
        type=OrderType.LIMIT if price else OrderType.MARKET,
        price=price,
        qty=orig_sz,
        filled_qty=filled_qty,
        status=status,
        created_at=created_at,
        updated_at=None,
    )


def map_position(hl_asset_pos: dict, exchange: str) -> Position:
    """Map Hyperliquid assetPosition dict to internal Position model."""
    pos = hl_asset_pos.get("position", hl_asset_pos)
    szi = Decimal(str(pos["szi"]))
    side = PositionSide.LONG if szi > 0 else PositionSide.SHORT
    size = abs(szi)

    leverage_info = pos.get("leverage") or {}
    leverage_val = leverage_info.get("value")
    leverage = Decimal(str(leverage_val)) if leverage_val is not None else None

    upnl_raw = pos.get("unrealizedPnl")
    unrealized_pnl = Decimal(str(upnl_raw)) if upnl_raw is not None else None

    return Position(
        market=_normalize_market(pos["coin"]),
        exchange=exchange,
        side=side,
        size=size,
        entry_price=Decimal(str(pos["entryPx"])),
        mark_price=None,  # not in assetPositions; available via separate meta endpoint
        leverage=leverage,
        unrealized_pnl=unrealized_pnl,
        opened_at=None,
    )


def map_fill_to_trade(hl_fill: dict, exchange: str) -> Trade:
    """Map a closing Hyperliquid fill to an internal Trade (closed position).

    NOTE: entry_price is not available in fill data. We use exit_price as a
    stand-in so that PnL % will be marked as approximate in notifications.
    """
    dir_str = hl_fill.get("dir", "")
    if "Long" in dir_str:
        side = PositionSide.LONG
    elif "Short" in dir_str:
        side = PositionSide.SHORT
    else:
        # Fallback: infer from fill side field
        side = PositionSide.LONG if hl_fill.get("side") == _HL_BUY else PositionSide.SHORT

    exit_price = Decimal(str(hl_fill["px"]))
    closed_pnl = Decimal(str(hl_fill.get("closedPnl", "0")))
    fill_id = str(hl_fill.get("tid") or hl_fill.get("oid") or "0")

    return Trade(
        id=fill_id,
        exchange=exchange,
        market=_normalize_market(hl_fill["coin"]),
        side=side,
        size=Decimal(str(hl_fill["sz"])),
        entry_price=exit_price,  # approximation — entry not in fill data
        exit_price=exit_price,
        realised_pnl=closed_pnl,
        opened_at=None,
        closed_at=_unix_ms_to_utc(hl_fill["time"]),
    )


async def _fills_by_time(info, address: str, start_ms: int) -> list[dict]:
    """Fetch fills since start_ms. Falls back to user_fills() if SDK lacks time filtering."""
    try:
        return await asyncio.to_thread(info.user_fills_by_time, address, start_ms)
    except AttributeError:
        all_fills: list[dict] = await asyncio.to_thread(info.user_fills, address)
        return [f for f in all_fills if f.get("time", 0) >= start_ms]


class HyperliquidAdapter(ExchangeAdapter):
    """Exchange adapter for Hyperliquid DEX.

    Authentication: read-only endpoints require only a wallet address (0x…).
    No private key is needed for monitoring.

    Sync SDK: hyperliquid-python-sdk uses synchronous requests internally. All
    SDK calls are dispatched via asyncio.to_thread() to avoid blocking the loop.

    Cancelled orders: Hyperliquid has no bulk cancelled-order history endpoint.
    Orders that disappear without appearing in fills will exhaust the
    disappeared_pending retry queue and be reported as DISAPPEARED_UNKNOWN.

    Entry price in closed trades: fill data contains only the exit price.
    entry_price is set to exit_price as an approximation; PnL % is therefore
    an estimate based on (realised_pnl / (exit_price * size)).
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._info = None

    @property
    def exchange_name(self) -> str:
        return "Hyperliquid"

    async def connect(self) -> None:
        try:
            from hyperliquid.info import Info  # type: ignore[import]
        except ImportError as exc:
            raise ExchangeConnectionError(
                "hyperliquid-python-sdk is not installed; add it to requirements.txt"
            ) from exc

        base_url = _TESTNET_URL if self._config.hyperliquid_testnet else _MAINNET_URL
        self._info = Info(base_url, skip_ws=True)
        logger.info(
            "Hyperliquid adapter initialised",
            extra={"network": "testnet" if self._config.hyperliquid_testnet else "mainnet"},
        )

    async def disconnect(self) -> None:
        self._info = None
        logger.info("Hyperliquid adapter disconnected")

    def _info_or_raise(self):
        if self._info is None:
            raise ExchangeConnectionError(
                "HyperliquidAdapter not connected — call connect() first"
            )
        return self._info

    def _address(self) -> str:
        addr = self._config.hyperliquid_wallet_address
        if not addr:
            raise ExchangeConnectionError("HYPERLIQUID_WALLET_ADDRESS is not configured")
        return addr

    async def get_open_orders(self) -> list[Order]:
        info = self._info_or_raise()
        address = self._address()
        try:
            raw: list[dict] = await asyncio.to_thread(info.open_orders, address)
        except Exception as exc:
            raise ExchangeConnectionError(f"get_open_orders failed: {exc}") from exc

        orders: list[Order] = []
        for item in raw:
            try:
                orders.append(map_order(item, self.exchange_name))
            except Exception:
                logger.debug(
                    "Skipping unparseable open order",
                    extra={"item": str(item)[:120]},
                )
        logger.debug("Fetched open orders", extra={"count": len(orders)})
        return orders

    async def get_positions(self) -> list[Position]:
        info = self._info_or_raise()
        address = self._address()
        try:
            state: dict = await asyncio.to_thread(info.user_state, address)
        except Exception as exc:
            raise ExchangeConnectionError(f"get_positions failed: {exc}") from exc

        positions: list[Position] = []
        for asset_pos in state.get("assetPositions", []):
            pos = asset_pos.get("position", {})
            if Decimal(str(pos.get("szi", "0"))) == 0:
                continue
            try:
                positions.append(map_position(asset_pos, self.exchange_name))
            except Exception:
                logger.debug(
                    "Skipping unparseable position",
                    extra={"coin": pos.get("coin")},
                )
        logger.debug("Fetched positions", extra={"count": len(positions)})
        return positions

    async def get_orders_history(self, since: datetime) -> list[Order]:
        """Return recently filled orders reconstructed from user fills.

        Cancelled orders are not available via bulk history. They will be
        handled by the disappeared_pending retry logic in the event engine
        and ultimately reported as DISAPPEARED_UNKNOWN if not found here.
        """
        info = self._info_or_raise()
        address = self._address()
        start_ms = int(since.timestamp() * 1000)
        try:
            fills = await _fills_by_time(info, address, start_ms)
        except Exception as exc:
            raise ExchangeConnectionError(f"get_orders_history failed: {exc}") from exc

        orders: list[Order] = []
        seen_oids: set[str] = set()
        for fill in fills:
            oid = str(fill.get("oid", ""))
            if not oid or oid in seen_oids:
                continue
            seen_oids.add(oid)
            try:
                side = OrderSide.BUY if fill.get("side") == _HL_BUY else OrderSide.SELL
                ts = fill.get("time")
                orders.append(
                    Order(
                        id=oid,
                        exchange=self.exchange_name,
                        market=_normalize_market(fill["coin"]),
                        side=side,
                        type=OrderType.LIMIT,
                        price=Decimal(str(fill["px"])),
                        qty=Decimal(str(fill["sz"])),
                        filled_qty=Decimal(str(fill["sz"])),
                        status=OrderStatus.FILLED,
                        created_at=_unix_ms_to_utc(ts) if ts else datetime.now(timezone.utc),
                        updated_at=_unix_ms_to_utc(ts) if ts else None,
                    )
                )
            except Exception:
                logger.debug(
                    "Skipping unparseable fill in orders_history",
                    extra={"fill": str(fill)[:120]},
                )
        logger.debug("Fetched orders history (from fills)", extra={"count": len(orders)})
        return orders

    async def get_positions_history(self, since: datetime) -> list[Trade]:
        """Return closed position trades: fills where closedPnl != 0."""
        info = self._info_or_raise()
        address = self._address()
        start_ms = int(since.timestamp() * 1000)
        try:
            fills = await _fills_by_time(info, address, start_ms)
        except Exception as exc:
            raise ExchangeConnectionError(f"get_positions_history failed: {exc}") from exc

        trades: list[Trade] = []
        for fill in fills:
            if Decimal(str(fill.get("closedPnl", "0"))) == 0:
                continue
            try:
                trades.append(map_fill_to_trade(fill, self.exchange_name))
            except Exception:
                logger.debug(
                    "Skipping unparseable position close fill",
                    extra={"fill": str(fill)[:120]},
                )
        logger.debug("Fetched positions history", extra={"count": len(trades)})
        return trades

    async def get_trades(self, since: datetime) -> list[Trade]:
        """Return all individual fill events since the given time."""
        info = self._info_or_raise()
        address = self._address()
        start_ms = int(since.timestamp() * 1000)
        try:
            fills = await _fills_by_time(info, address, start_ms)
        except Exception as exc:
            raise ExchangeConnectionError(f"get_trades failed: {exc}") from exc

        trades: list[Trade] = []
        for fill in fills:
            try:
                trades.append(map_fill_to_trade(fill, self.exchange_name))
            except Exception:
                logger.debug(
                    "Skipping unparseable fill in get_trades",
                    extra={"fill": str(fill)[:120]},
                )
        logger.debug("Fetched trades", extra={"count": len(trades)})
        return trades
